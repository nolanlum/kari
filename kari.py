import functools
import html
import json
import logging
import os
import re
import select
import socket
import ssl
import string
import threading
import traceback
import time
from asyncio import get_event_loop
from collections import defaultdict
from itertools import zip_longest
from hashlib import md5
from io import BytesIO
from os.path import basename

import attr
import requests
import websockets
import irc.client
import yaml
import emoji
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from key_view import KeyViewList


LETTERS_NUMBERS = string.ascii_letters + string.digits

REQUEST_TIMEOUT = 10
UPLOAD_REQUEST_TIMEOUT = 60


log = logging.getLogger('kari')


class Disconnection(Exception):
    pass


@attr.s
class IRCServer:
    conf = attr.ib()
    conn = attr.ib()

    @property
    def name(self):
        return self.conf['name']


@attr.s
class IRCChannel:
    name = attr.ib()
    server = attr.ib()

    @classmethod
    def get_full_name(cls, server_name, channel_name):
        return f'{server_name}:{channel_name}'

    @property
    def full_name(self):
        return self.get_full_name(self.server.conf['name'], self.name)


@attr.s
class Bridge:
    slack_channel = attr.ib()
    irc_channel = attr.ib()


class Kari:
    def __init__(self):
        with open('config.yaml') as fh:
            config = yaml.safe_load(fh)

        self.mirror_conf = config['slack']['file_mirror']

        # Set up Slack
        resp = requests.post(
            'https://slack.com/api/users.list',
            headers={'Authorization': f"Bearer {config['slack']['avatar_pilfering_token']}"},
            timeout=REQUEST_TIMEOUT,
        ).json()

        self.irc_username_to_slack_avatar = {u['name']: u['profile']['image_512'] for u in resp['members']}

        # Slack client doesn't uses sessions -> keepalive, just use requests.
        self.slack_session = requests.Session()
        # Oh, Requests...
        self.slack_session.request = functools.partial(self.slack_session.request, timeout=REQUEST_TIMEOUT)
        adapter = HTTPAdapter(max_retries=Retry(
            total=3,
            backoff_factor=0.1,
            allowed_methods=None,
        ))

        self.slack_session.mount('http://', adapter)
        self.slack_session.mount('https://', adapter)

        self.slack_session.headers.update({
            'Authorization': f"Bearer {config['slack']['token']}"
        })
        self.slack_socket_token_header = {
            'Authorization': f"Bearer {config['slack']['websocket_token']}"
        }

        self.slack_channels = KeyViewList(self.slack_api('conversations.list')['channels'])
        users = KeyViewList(self.slack_api('users.list')['members'])

        self.slack_channel_by_name = self.slack_channels.register_itemgetter('name')
        self.slack_channel_by_id = self.slack_channels.register_itemgetter('id')

        self.slack_own_user_id = [u['id'] for u in users if u['name'] == config['slack']['username']][0]
        self.slack_real_user_id = [u['id'] for u in users if u['name'] == config['slack']['realuser_username']][0]
        self.slack_user_by_id = users.register_itemgetter('id')

        self._init_error_channel(config['slack']['errors'][1:])

        self.slack_ws = None

        self.bridges = KeyViewList()
        self.bridge_by_irc_full_name = self.bridges.register_attrgetter('irc_channel.full_name')
        self.bridge_by_slack_channel = self.bridges.register_attrgetter('slack_channel')

        self.irc_servers = KeyViewList()
        self.irc_server_by_conn = self.irc_servers.register_attrgetter('conn')
        self.irc_server_by_name = self.irc_servers.register_attrgetter('name')

        # Topic data is used for establishing PM linkage as they aren't explicitly mapped
        irc_server_to_pm_channels = defaultdict(list)
        for channel in self.slack_channels:
            if 'topic' in channel:
                pm_config = self._extract_pm_config(channel['topic']['value'])
                if pm_config:
                    log.info(f'Found PM configuration for {channel["name"]}: {pm_config}')
                    irc_server_to_pm_channels[
                        pm_config['server']
                    ].append(
                        (pm_config['target'], '#' + channel['name']),
                    )

        for irc_conf in config['irc']:
            irc_server = IRCServer(conf=irc_conf, conn=None)
            self.irc_servers.append(irc_server)

            for irc_channel_name, slack_channel_name in irc_conf['channels'].items():
                slack_channel = self.slack_channel_by_name[slack_channel_name[1:]]
                if not slack_channel['is_member']:
                    self._slack_join(slack_channel['id'])
                irc_channel = IRCChannel(name=irc_channel_name, server=irc_server)
                bridge = Bridge(slack_channel=slack_channel_name, irc_channel=irc_channel)
                log.debug(f'Setting up bridge: {bridge}')

                self.bridges.append(bridge)

            for irc_target_user_name, slack_channel_name in irc_server_to_pm_channels[irc_conf['name']]:
                slack_channel = self.slack_channel_by_name[slack_channel_name[1:]]
                if not slack_channel['is_member']:
                    self._slack_join(slack_channel['id'])
                irc_channel = IRCChannel(name=irc_target_user_name, server=irc_server)
                bridge = Bridge(slack_channel=slack_channel_name, irc_channel=irc_channel)
                log.debug(f'Setting up bridge: {bridge}')

                self.bridges.append(bridge)

        # Set up IRC
        irc_thread = threading.Thread(
            target=self.irc_connect,
            args=(config,),
            daemon=True,
        )
        irc_thread.start()
        threading.Thread(
            target=self.thread_bomb, args=(irc_thread,), daemon=True,
        ).start()

        self.loop = get_event_loop()

        # Cede control to asyncio -> slack.
        self.loop.run_until_complete(self.slack_connect())

    def _init_error_channel(self, error_channel_name):
        error_channel = self.slack_channel_by_name.get(error_channel_name)

        if not error_channel:
            raise ValueError(f'Configured error channel #{error_channel_name} does not exist')

        if not error_channel['is_member']:
            raise ValueError(f"Bot not joined to configured slack channel #{error_channel_name}")
        self.slack_error_channel_id = error_channel['id']

    def thread_bomb(self, target):
        target.join()
        os._exit(1)

    def slack_api(self, endpoint, params=None, headers=None):
        before = time.time()
        resp = self.slack_session.post(
            f'https://slack.com/api/{endpoint}',
            data=params,
            headers=headers,
        )
        duration = time.time() - before
        log.info(f"API request took {duration}s")
        resp.raise_for_status()
        resp = resp.json()

        if not resp['ok']:
            log.error('Error received from Slack API: %s', resp)
            if endpoint != 'chat.postMessage':
                # Hopefully no loops are created.
                self.slack_error(f'Error from Slack API ({endpoint}): ```{resp}```')

        return resp

    def irc_connect(self, config):
        # XXX: Backoff on failures?
        while True:
            reactor = irc.client.Reactor()

            try:
                for irc_server in self.irc_servers:
                    if irc_server.conf['ssl']:
                        ssl_ctx = ssl.create_default_context()
                        ssl_ctx.check_hostname = False
                        wrapper = functools.partial(ssl_ctx.wrap_socket, server_hostname=irc_server.conf['hostname'])
                    else:
                        def wrapper(x): return x

                    irc_server.conn = reactor.server().connect(
                        server=irc_server.conf['hostname'],
                        port=int(irc_server.conf['port']),
                        nickname=irc_server.conf['nickname'],
                        password=irc_server.conf['password'],
                        connect_factory=irc.connection.Factory(wrapper=wrapper),
                    )

                # Necessary because the view key is conn which we just assigned to
                self.irc_servers.refresh()

                reactor.add_global_handler('pubmsg', self.irc_message)
                reactor.add_global_handler('privmsg', self.irc_message)
                reactor.add_global_handler('action', self.irc_message)
                # reactor will actually not stop for this by default, if the server says it's over,
                # reactor will just continue processing an empty list of sockets.
                reactor.add_global_handler('disconnect', self.irc_disconnect)

                # No reason to poll so hard, we should believe select(3) works.
                reactor.process_forever(timeout=60.0)
            except (select.error, Disconnection) as ex:
                log.error(f'Received exception from irc: {ex}: {str(ex)}, reconnecting')

                for sock in reactor.sockets:
                    sock.shutdown(socket.SHUT_RDWR)
                    sock.close()

    async def slack_connect(self):
        while True:
            try:
                resp = self.slack_api(
                    'apps.connections.open',
                    headers=self.slack_socket_token_header,
                )
            except requests.exceptions.HTTPError as ex:
                log.error(f'Exception calling apps.connections.open: {ex}, reconnecting')
                # Will this work if we had troubles with apps.connections.open? No idea!
                try:
                    self.slack_error(f'Exception calling apps.connections.open: {ex}, reconnecting')
                except requests.exceptions.HTTPError as ex2:
                    log.error(f'Exception reporting exception: {ex2}')
                break
            except Exception as ex:
                log.error(f'Last resort Exception calling apps.connections.open: {ex}, reconnecting')
                exc_text = traceback.format_exc()
                print(exc_text)
                self.slack_error(f'Last resort Exception calling apps.connections.open: {ex}, reconnecting\n```{exc_text}```')
                break

            try:
                async with websockets.connect(resp['url']) as ws:
                    self.slack_ws = ws
                    while True:
                        msg = await ws.recv()
                        msg = json.loads(msg)
                        await self.slack_event(msg)
            except Disconnection:
                log.error('Slack remote wants to disconnect gracefully, reconnecting')
            except websockets.exceptions.ConnectionClosed as ex:
                log.error(f'Exception from websockets: {ex}, reconnecting')
                self.slack_error(f'Exception from websockets: {ex}, reconnecting')
            except ConnectionError as ex:
                log.error(f'Exception from sockets: {ex}, reconnecting')
                self.slack_error(f'Exception from sockets: {ex}, reconnecting')
            except OSError as ex:
                # We don't look at errno too hard because it isn't exposed
                # in the case of a "combined exception" (asyncio stuff).
                log.error(f'OSError: (maybe socket related): {ex}, reconnecting')
                self.slack_error(f'OSError: (maybe socket related): {ex}, reconnecting')
            except Exception as ex:
                log.error(f'Last resort Exception: {ex}, reconnecting')
                exc_text = traceback.format_exc()
                print(exc_text)
                self.slack_error(f'Last resort Exception: {ex}, reconnecting\n```{exc_text}```')

    def _extract_pm_config(self, topic):
        m = re.fullmatch(r'(?P<server>[^/]+)/(?P<target>[^/]+)', topic)
        if m:
            return m.groupdict()
        return None

    def _slack_join(self, channel_id):
        self.slack_api('conversations.join', {'channel': channel_id})

    def _slack_invite(self, channel_id):
        self.slack_api(
            'conversations.invite',
            {
                'channel': channel_id,
                'users': self.slack_real_user_id,
            },
        )

    def irc_disconnect(self, conn, event):
        # Throw control back to irc_connect
        log.debug('Got disconnect event, raising exception')
        raise Disconnection()

    def irc_message(self, conn, event):
        if not self.slack_ws:
            log.debug('Ignoring IRC message because Slack is not yet connected')
            return

        irc_server = self.irc_server_by_conn[conn]

        # Contextual text transformations
        text = event.arguments[0]
        if event.type == 'action':
            text = '_' + text + '_'
        nick = event.source.nick
        if event.source.nick in irc_server.conf.get('deprefix', []):
            m = re.match(r'<([^>]+)> (.*)', text)
            if m:
                nick = m[1]
                text = m[2]

        if event.source.nick.startswith('*'):
            self.slack_error("Message from internal {}: {}".format(
                event.source.nick,
                reformat_irc(text),
            ))
            return

        is_public = event.target.startswith('#')

        slack_channel_name = None
        peer = None
        if is_public:
            peer = event.target
        else:
            peer = event.source.nick

        full_name = IRCChannel.get_full_name(
            irc_server.conf['name'],
            peer,
        )
        log.debug(f'Looking for bridge for {full_name}')
        bridge = self.bridge_by_irc_full_name.get(full_name)

        if bridge:
            log.debug(f'Found bridge {bridge}')
            slack_channel_name = bridge.slack_channel[1:]
        elif not is_public:
            # Time to create channel!
            slack_channel_name = f'dm-{event.source.nick.lower()[:18]}'

            if self.slack_channel_by_name.get(slack_channel_name):
                log.error('Tried to create a DM channel for %s, but %s already exists and is configured for something else', event.source.nick, slack_channel_name)
                self.slack_error(f'Tried to create a DM channel for {event.source.nick}, but {slack_channel_name} already exists and is configured for something else')
                return

            log.debug(f'Creating DM channel {slack_channel_name}')

            resp = self.slack_api(
                'conversations.create',
                {
                    'name': slack_channel_name,
                    'validate': True,
                },
            )
            if not resp['ok']:
                return

            channel = resp['channel']
            self.slack_channels.append(channel)

            resp = self.slack_api(
                'conversations.setTopic',
                {
                    'channel': channel['id'],
                    'topic': f'{irc_server.conf["name"]}/{event.source.nick.lower()}'
                },
            )
            if not resp['ok']:
                return

            # We will receive a channel_topic event. Complete the bridge registration there, in slack_message

            # Invite after configuring otherwise we will complain about getting a join message from the real user for a channel not yet configured.
            self._slack_invite(channel['id'])

        if not slack_channel_name:
            log.error('%s not configured for slack, ignoring', event.target)
            return

        # Can't impersonate user from RTM API, have to use postMessage.
        self.slack_api(
            "chat.postMessage",
            {
                'username': nick,
                'text': reformat_irc(text),
                'channel': self.slack_channel_by_name[slack_channel_name]['id'],
                'icon_url': self.irc_username_to_slack_avatar.get(
                    irc_server.conf.get('users', {}).get(
                        nick
                    ) or nick.lower(),
                    f'https://avatars.dicebear.com/api/personas/{md5(nick.lower().encode("utf-8")).hexdigest()}.png',
                ),
                'unfurl_links': True,
                'unfurl_media': True,
                'parse': 'full',
            }
        )

    def slack_error(self, error_msg):
        self.slack_api(
            "chat.postMessage",
            {
                'text': error_msg,
                'channel': self.slack_error_channel_id,
            }
        )

    def mirror_upload(self, slack_channel, slack_ts, irc_conn, irc_channel, slack_url):
        log.info(f'Mirroring {slack_url}')

        resp = self.slack_session.get(slack_url)

        resp = requests.post(
            self.mirror_conf['host'],
            data={'k': self.mirror_conf['key']},
            files={'f': (basename(slack_url), BytesIO(resp.content), resp.headers['Content-Type'])},
            timeout=UPLOAD_REQUEST_TIMEOUT,
        ).json()

        self.slack_api(
            "chat.postMessage",
            {
                'username': 'pshuu_mirror',
                'text': resp['share_url'],
                'channel': slack_channel,
                'thread_ts': slack_ts,
                'reply_broadcast': True,
                'unfurl_links': False,
                'unfurl_media': False,
            }
        )

    async def slack_event(self, msg):
        type_ = msg.get('type')

        if type_ == 'events_api' and msg['payload'].get('type') == 'event_callback':
            # I love doubly enveloped messages
            self.slack_message(msg['payload']['event'])
        elif type_ == 'hello':
            debug_info = msg['debug_info']
            log.debug(f"Connected to {debug_info['host']}, connection should last {debug_info['approximate_connection_time']}s")
        elif type_ == 'disconnect':
            raise Disconnection()

        if 'envelope_id' in msg:
            await self.slack_ws.send(json.dumps({'envelope_id': msg['envelope_id']}))

    def slack_message(self, msg):
        type_ = msg.get('type')
        subtype = msg.get('subtype')

        if type_ == 'message':
            # Error channel?
            if msg['channel'] == self.slack_error_channel_id:
                return

            slack_channel_name = '#' + self.slack_channel_by_id[msg['channel']]['name']
            bridge = self.bridge_by_slack_channel.get(slack_channel_name)

            # Slack-only channel?
            # If it's a channel_topic, we could be reconfiguring a PM room
            if not bridge:
                if subtype == 'channel_topic':
                    irc_conn = None
                    irc_channel_name = None
                else:
                    log.error('%s not configured for irc, ignoring', slack_channel_name)
                    self.slack_error(f'{slack_channel_name} not configured for irc, ignoring')
                    return
            else:
                irc_conn = bridge.irc_channel.server.conn
                irc_channel_name = bridge.irc_channel.name

            if subtype in ('message_replied', 'thread_broadcast', None):
                # Normal message

                # TODO: For non-broadcast replies, fetch the other replies from Slack, they aren't sent with this message any more
                # TODO: Only quotepost on the first reply or if a period of time has passed.

                # We want to post any shares before the text we're commenting on them with
                if 'files' in msg:
                    for file in msg['files']:
                        threading.Thread(
                            target=self.mirror_upload,
                            args=(msg['channel'], msg['ts'], irc_conn, irc_channel_name, file['url_private']),
                            daemon=True,
                        ).start()

                # Slack only provides 'root' for a thread broadcast message and
                # provides nothing for message replies, so that sucks.
                if 'root' in msg:
                    root = msg['root']
                    if 'type' in root and root['type'] == 'message':
                        quoting_format = get_quoting_format(root.get('subtype'))
                        # No user if bot
                        if 'user' in root:
                            nick = self.slack_user_by_id[root['user']]['name']
                        else:
                            nick = root['username']

                        text = '\n'.join(
                            quoting_format.format(nick=nick, message=reformat_slack(line))
                            for line in root['text'].split('\n')
                        )

                        privmsg(irc_conn, irc_channel_name, text, should_reformat=False)

                # Now send the actual message
                if 'text' in msg and msg['text'] != '':
                    privmsg(irc_conn, irc_channel_name, msg['text'])

            elif subtype == 'me_message':
                irc_conn.action(irc_channel_name, emojize(msg['text']))
            elif subtype == 'channel_topic':
                log.info('Channel topic changed event: %s', msg)

                # Is this a PM bridge?
                if irc_channel_name and irc_channel_name.startswith('#'):
                    return

                # Deregister old mapping if there is any
                if bridge:
                    log.info('Removing mapping between Slack %s and IRC %s', slack_channel_name, irc_channel_name)
                    self.bridges.remove(bridge)

                # Register new mapping if any
                pm_config = self._extract_pm_config(msg['topic'])
                if pm_config:
                    irc_server = self.irc_server_by_name.get(pm_config['server'])

                    if not irc_server:
                        log.error('Nonexistent server specified in Slack channel topic: %s to configure %s', pm_config['server'], slack_channel_name)
                        return

                    log.info('Adding mapping between Slack %s and IRC %s', slack_channel_name, pm_config['target'])
                    irc_channel = IRCChannel(name=pm_config['target'], server=irc_server)
                    bridge = Bridge(slack_channel=slack_channel_name, irc_channel=irc_channel)

                    self.bridges.append(bridge)
            elif subtype in ('bot_message',):
                pass
            else:
                log.debug(f'Not handling message event subtype {subtype}')
        elif type_ == 'channel_joined':
            log.info('Channel joined event: %s', msg)
            slack_channel_name = '#' + self.slack_channel_by_id[msg['channel']['id']]['name']

            # Register new mapping if any
            pm_config = self._extract_pm_config(msg['channel']['topic']['value'])
            if pm_config:
                irc_server = self.irc_server_by_name.get(pm_config['server'])

                if not irc_server:
                    log.error('Nonexistent server specified in Slack channel topic: %s to configure %s', pm_config['server'], slack_channel_name)
                    return

                log.info('Adding mapping between Slack %s and IRC %s', slack_channel_name, pm_config['target'])
                irc_channel = IRCChannel(name=pm_config['target'], server=irc_server)
                bridge = Bridge(slack_channel=slack_channel_name, irc_channel=irc_channel)

                self.bridges.append(bridge)
        elif type_ == 'channel_created':
            log.info('Channel created event: %s', msg)
            self.slack_channels.append(msg['channel'])
        elif type_ == 'channel_rename':
            log.info('Channel renamed event: %s', msg)

            slack_channel_name = '#' + self.slack_channel_by_id[msg['channel']['id']]['name']
            bridge = self.bridge_by_slack_channel.get(slack_channel_name)
            if bridge:
                log.debug(f'Renaming bridge {bridge}')
                bridge.slack_channel = '#' + msg['channel']['name']
                self.bridge_by_slack_channel.refresh()

            self.slack_channel_by_id[msg['channel']['id']]['name'] = msg['channel']['name']
            self.slack_channel_by_name.refresh()
        elif type_ == 'channel_deleted':
            log.info('Channel deleted event: %s', msg)

            slack_channel_name = '#' + self.slack_channel_by_id[msg['channel']]['name']
            bridge = self.bridge_by_slack_channel.get(slack_channel_name)
            if bridge:
                log.debug(f'Removing bridge {bridge}')
                self.bridges.remove(bridge)

            self.slack_channels.remove(
                self.slack_channel_by_id[msg['channel']]
            )
        elif type_ == 'goodbye':
            raise Disconnection()
        elif type_ == 'error':
            log.error('Error from Slack stream: %s', msg)
            self.slack_error(f'Error from Slack stream: ```{msg}```')
        elif type_ in ('hello',):
            pass
        else:
            log.debug(f"Not handling event type {type_}")


def apply_span(msg, span, md_char):
    begin, end = span
    if end is None:
        end = len(msg)
        msg.append('')

    # Move the bold in if the leading character is a space,
    # for which Slack won't bold the span.
    real_begin = begin
    while msg[begin + 1] == ' ':
        begin += 1

    # Insert the substitute character with a zero width space if necessary.
    if begin == 0 or msg[begin - 1] not in LETTERS_NUMBERS:
        msg[begin] = md_char
    else:
        msg[begin] = '\u200b' + md_char

    # Add back the space.
    if begin != real_begin:
        msg[real_begin] = ' '

    real_end = end
    while msg[end - 1] == ' ':
        end -= 1

    if end == len(msg) - 1 or msg[end + 1] not in LETTERS_NUMBERS:
        msg[end] = md_char
    else:
        msg[end] = md_char + '\u200b'

    if end != real_end:
        msg[real_end] = ' '


def reformat_irc(msg):
    orig_msg = msg
    msg = list(msg)

    # We could alternatively search for pairs and then deal with
    # any final ones separately
    bold_idxs = (m.start() for m in re.finditer('\x02', orig_msg))
    bold_spans = zip_longest(*[iter(bold_idxs)] * 2)

    for span in bold_spans:
        apply_span(msg, span, '*')

    # Also convert underlines here
    italic_idxs = (m.start() for m in re.finditer('\x1d|\x1f', orig_msg))
    italic_spans = zip_longest(*[iter(italic_idxs)] * 2)

    for span in italic_spans:
        apply_span(msg, span, '_')

    # TODO: strip color formatting

    return ''.join(msg)


def get_quoting_format(inner_subtype):
    quoting_format = None
    if inner_subtype is None or inner_subtype in ('bot_message', 'thread_broadcast'):
        quoting_format = '<{nick}> {message}'
    elif inner_subtype == 'me_message':
        quoting_format = '* {nick} {message}'

    return quoting_format


def delink(match):
    inner = match.group(1)

    # Not handling user references (startswith('@')) because why would you do that
    if inner.startswith('#'):
        _, name = inner.split('|')
        return '#' + name
    else:
        # URL: maybe resolved, maybe not
        if '|' in inner:
            full_url, display_url = inner.split('|')
            return full_url
        return inner


def emojize(msg):
    return emoji.emojize(msg, language='alias')


def reformat_slack(msg):
    # Remove linky entities. Users, channels, hyperlinks.
    msg = re.sub(r'<([^>]+)>', delink, msg)
    # Convert Markdown bold and italics to equivalent
    # These have to have come in pairs so it's slightly easier
    # We accept either a leading space, trailing space, or neither, but not both.
    msg = re.sub(r'(?<![a-zA-Z0-9])\*([^ \*][^\*]*|[^\*][^ \*]*)\*(?![a-zA-Z0-9])', '\x02\\1\x02', msg)
    msg = re.sub(r'(?<![a-zA-Z0-9])_([^ _][^_]*|[^_][^ _]*)_(?![a-zA-Z0-9])', '\x1d\\1\x1d', msg)
    # Remove emoji shortcodes and url escapes.
    msg = emojize(html.unescape(msg))
    return msg


def privmsg(irc_conn, irc_channel, msg, should_reformat=True):
    if should_reformat:
        msg = reformat_slack(msg)
    msgs = msg.split('\n')

    bytes_available = 512 - len('PRIVMSG {} :\r\n'.format(irc_channel))
    for msg in msgs:
        msg_bytes = irc_conn.encode(msg)

        while len(msg_bytes) > bytes_available:
            space_idx = msg_bytes.rfind(b' ', 0, bytes_available)
            fragment = msg_bytes[:space_idx]
            msg_bytes = msg_bytes[space_idx + 1:]
            irc_conn.privmsg(irc_channel, fragment.decode(irc_conn.transmit_encoding))
        irc_conn.privmsg(irc_channel, msg_bytes.decode(irc_conn.transmit_encoding))


if __name__ == '__main__':
    # irc...
    logging.addLevelName(8, 'DEBUG')
    logging.basicConfig(
        format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        level=0,
    )
    Kari()
