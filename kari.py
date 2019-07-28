import html
import json
import logging
import os
import re
import select
import signal
import socket
import ssl
import string
import threading
import time
from asyncio import get_event_loop
from collections import defaultdict
from itertools import zip_longest
from io import BytesIO
from os.path import basename
from urllib.parse import urlencode

import requests
import websockets
import irc.client
import yaml
import emoji


LETTERS_NUMBERS = string.ascii_letters + string.digits


log = logging.getLogger('kari')


class IRCDisconnection(Exception):
    pass


class Kari:
    def __init__(self):
        config = yaml.safe_load(open('config.yaml'))

        self.mirror_conf = config['slack']['file_mirror']

        # Set up Slack
        resp = requests.post(
            'https://slack.com/api/users.list', headers={'Authorization': f"Bearer {config['slack']['avatar_pilfering_token']}"}
        ).json()

        self.irc_username_to_slack_avatar = {u['name']: u['profile']['image_512'] for u in resp['members']}

        # Slack client doesn't uses sessions -> keepalive, just use requests.
        self.slack_session = requests.Session()
        self.slack_session.headers.update({
            'Authorization': f"Bearer {config['slack']['token']}"
        })
        self.slack_user_token_header = {
            'Authorization': f"Bearer {config['slack']['user_token']}"
        }
        users_resp = self.slack_api('users.list')
        channels_resp = self.slack_api('conversations.list')

        self.slack_channel_name_to_id = {c['name']: c['id'] for c in channels_resp['channels']}
        self.slack_channel_id_to_name = {c['id']: c['name'] for c in channels_resp['channels']}

        # Topic data is used for establishing PM linkage as they aren't explicitly mapped
        network_to_pm_channels = defaultdict(list)
        for channel in channels_resp['channels']:
            if 'topic' in channel and '/' in channel['topic']['value']:
                network, target = channel['topic']['value'].split('/', 1)
                network_to_pm_channels[network].append(('#' + channel['name'], target))

        self.slack_own_user_id = [u['id'] for u in users_resp['members'] if u['name'] == config['slack']['username']][0]
        self.slack_user_id_to_name = {u['id']: u['name'] for u in users_resp['members']}

        self.slack_pending_thread_reply = None

        slack_channel_name_to_membership = {c['name']: c['is_member'] for c in channels_resp['channels']}

        if not slack_channel_name_to_membership[config['slack']['errors'][1:]]:
            raise ValueError(f"Bot not joined to configured slack channel {config['slack']['errors']}")
        self.slack_error_channel = self.slack_channel_name_to_id[config['slack']['errors'][1:]]

        self.slack_ws = None

        # Set up IRC
        irc_thread = threading.Thread(
            target=self.irc_connect,
            args=(config, network_to_pm_channels, slack_channel_name_to_membership),
            daemon=True,
        )
        irc_thread.start()
        threading.Thread(
            target=self.thread_bomb, args=(irc_thread,), daemon=True,
        ).start()

        self.loop = get_event_loop()

        # Cede control to asyncio -> slack.
        self.loop.run_until_complete(self.slack_connect())

    def thread_bomb(self, target):
        target.join()
        os.kill(os.getpid(), signal.SIGTERM)

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
                self.slack_error(f'Error from Slack API: ```{resp}```')

        return resp

    def irc_connect(self, config, network_to_pm_channels, slack_channel_name_to_membership):
        while True:
            reactor = irc.client.Reactor()
            self.slack_channel_to_irc = {}
            self.irc_conn_to_conf = {}
            self.irc_conf_name_to_conn = {}

            for name, irc_conf in config['irc'].items():
                irc_conn = reactor.server().connect(
                    server=irc_conf['hostname'],
                    port=int(irc_conf['port']),
                    nickname=irc_conf['nickname'],
                    password=irc_conf['password'],
                    connect_factory=irc.connection.Factory(
                        wrapper=ssl.wrap_socket if irc_conf['ssl'] else lambda x: x
                    ),
                )

                irc_conf['name'] = name
                self.irc_conn_to_conf[irc_conn] = irc_conf
                self.irc_conf_name_to_conn[name] = irc_conn

                for irc_channel, slack_channel in irc_conf['channels'].items():
                    if not slack_channel_name_to_membership[slack_channel[1:]]:
                        raise ValueError(f'Bot not joined to configured slack channel {slack_channel}')

                    self.slack_channel_to_irc[slack_channel] = (irc_conn, irc_channel)

                for slack_channel, target in network_to_pm_channels[name]:
                    self.slack_channel_to_irc[slack_channel] = (irc_conn, target)
                    # Not entirely correct...
                    irc_conf['channels'][target] = slack_channel
                    slack_channel_name_to_membership[slack_channel[1:]] = True
                    log.info(f'Set up PM bridge between {slack_channel} and {target}')

            reactor.add_global_handler('pubmsg', self.irc_message)
            reactor.add_global_handler('privmsg', self.irc_message)
            reactor.add_global_handler('action', self.irc_message)
            # reactor will actually not stop for this, if the server says it's over,
            # reactor will just continue processing an empty list of sockets.
            reactor.add_global_handler('disconnect', self.irc_disconnect)

            try:
                # No reason to poll so hard, we should believe select(3) works.
                reactor.process_forever(timeout=60.0)
            except (select.error, IRCDisconnection) as ex:
                log.error(f'Received exception from irc: {str(ex)}, reconnecting')
                for sock in reactor.sockets:
                    sock.shutdown(socket.SHUT_RDWR)
                    sock.close()

    async def slack_connect(self):
        while True:
            resp = self.slack_api('rtm.connect')
            try:
                async with websockets.connect(resp['url'], ping_interval=60.0) as ws:
                    self.slack_ws = ws
                    while True:
                        msg = await ws.recv()
                        msg = json.loads(msg)
                        self.slack_message(msg)
            except websockets.exceptions.ConnectionClosed as ex:
                log.error(f'Exception from websockets: {ex}, reconnecting')
            except ConnectionError as ex:
                log.error(f'Exception from sockets: {ex}, reconnecting')
                self.slack_error(f'Exception from sockets: {ex}, reconnecting')
            except OSError as ex:
                # We don't look at errno too hard because it isn't exposed
                # in the case of a "combined exception" (asyncio stuff).
                log.error(f'OSError: (maybe socket related): {ex}, reconnecting')
                self.slack_error(f'OSError: (maybe socket related): {ex}, reconnecting')

    def irc_disconnect(self, conn, event):
        # Throw control back to irc_connect
        raise IRCDisconnection()

    def irc_message(self, conn, event):
        if not self.slack_ws:
            return

        irc_conf = self.irc_conn_to_conf[conn]

        text = event.arguments[0]
        if event.type == 'action':
            text = '_' + text + '_'

        is_public = event.target.startswith('#')

        channel_name = None
        if is_public:
            if event.target in irc_conf['channels']:
                channel_name = irc_conf['channels'][event.target][1:]
        else:
            if event.source.nick in irc_conf['channels']:
                channel_name = irc_conf['channels'][event.source.nick][1:]
            # Control message?
            elif event.source.nick.startswith('*'):
                self.slack_error("Message from internal {}: {}".format(
                    event.source.nick,
                    reformat_irc(text),
                ))
                return
            else:
                # Time to create channel!
                channel_name = f'dm-{event.source.nick.lower()[:18]}'

                if channel_name in self.slack_channel_name_to_id:
                    log.error('Tried to create a DM channel for %s, but %s already exists and is configured for something else', event.source.nick, channel_name)
                    self.slack_error(f'Tried to create a DM channel for {event.source.nick}, but {channel_name} already exists and is configured for something else')
                    return

                resp = self.slack_api(
                    'channels.create',
                    {
                        'name': channel_name,
                        'validate': True,
                    },
                    headers=self.slack_user_token_header,
                )
                if not resp['ok']:
                    log.error('Error from Slack API: %s', resp)
                    self.slack_error(f'Error from Slack API: ```{resp}```')
                    return

                channel_id = resp['channel']['id']
                self.slack_channel_name_to_id[channel_name] = channel_id
                self.slack_channel_id_to_name[channel_id] = channel_name

                resp = self.slack_api(
                    'channels.invite',
                    {
                        'channel': channel_id,
                        'user': self.slack_own_user_id,
                    },
                    headers=self.slack_user_token_header,
                )
                if not resp['ok']:
                    log.error('Error from Slack API: %s', resp)
                    self.slack_error(f'Error from Slack API: ```{resp}```')
                    return

                resp = self.slack_api(
                    'channels.setTopic',
                    {
                        'channel': channel_id,
                        'topic': f'{irc_conf["name"]}/{event.source.nick.lower()}'
                    },
                    headers=self.slack_user_token_header,
                )
                if not resp['ok']:
                    log.error('Error from Slack API: %s', resp)
                    self.slack_error(f'Error from Slack API: ```{resp}```')
                    return

                # For some reason, only url params are allowed on this one.
                self.slack_api('users.prefs.setNotifications?' + urlencode({
                    'channel_id': channel_id,
                    'name': 'desktop',
                    'value': 'everything',
                    'global': 0,
                }), headers=self.slack_user_token_header)
                if not resp['ok']:
                    log.error('Error from Slack API: %s', resp)
                    self.slack_error(f'Error from Slack API: ```{resp}```')
                    return

                self.slack_api('users.prefs.setNotifications?' + urlencode({
                    'channel_id': channel_id,
                    'name': 'mobile',
                    'value': 'everything',
                    'global': 0,
                }), headers=self.slack_user_token_header)
                if not resp['ok']:
                    log.error('Error from Slack API: %s', resp)
                    self.slack_error(f'Error from Slack API: ```{resp}```')
                    return

                self.slack_channel_to_irc['#' + channel_name] = (conn, event.source.nick)
                # Not entirely correct...
                irc_conf['channels'][event.source.nick] = '#' + channel_name

        if not channel_name:
            log.error('%s not configured for slack, ignoring', event.target)
            return

        # Can't impersonate user from RTM API, have to use postMessage.
        self.slack_api(
            "chat.postMessage",
            {
                'username': event.source.nick,
                'text': reformat_irc(text),
                'channel': self.slack_channel_name_to_id[channel_name],
                'icon_url': self.irc_username_to_slack_avatar.get(
                    irc_conf.get('users', {}).get(
                        event.source.nick
                    ) or event.source.nick.lower(),
                    '',
                ),
            }
        )

    def slack_error(self, error_msg):
        self.slack_api(
            "chat.postMessage",
            {
                'text': error_msg,
                'channel': self.slack_error_channel,
            }
        )

    def mirror_upload(self, slack_channel, slack_ts, irc_conn, irc_channel, slack_url):
        log.info(f'Mirroring {slack_url}')

        resp = self.slack_session.get(slack_url)

        resp = requests.post(
            self.mirror_conf['host'],
            data={'k': self.mirror_conf['key']},
            files={'f': (basename(slack_url), BytesIO(resp.content), resp.headers['Content-Type'])},
        ).json()

        irc_conn.privmsg(irc_channel, resp['share_url'])
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

    def slack_message(self, msg):
        type_ = msg.get('type')
        subtype = msg.get('subtype')

        if type_ == 'message':
            # Error channel?
            if msg['channel'] == self.slack_error_channel:
                return

            channel_name = '#' + self.slack_channel_id_to_name[msg['channel']]

            # Slack-only channel?
            if channel_name not in self.slack_channel_to_irc and subtype != 'channel_topic':
                log.error('%s not configured for irc, ignoring', channel_name)
                self.slack_error(f'{channel_name} not configured for irc, ignoring')
                return

            irc_conn, irc_channel = self.slack_channel_to_irc.get(channel_name) or (None, None)

            if subtype is None:
                if 'files' in msg:
                    for file in msg['files']:
                        threading.Thread(
                            target=self.mirror_upload,
                            args=(msg['channel'], msg['ts'], irc_conn, irc_channel, file['url_private']),
                            daemon=True,
                        ).start()

                # Very carefully only handle message shares
                # On successive shares we won't get recursive message unfurls in API
                # and neither in the real client. It could be looked into...
                if 'attachments' in msg:
                    for attachment in msg['attachments']:
                        if attachment['is_share'] and attachment['is_msg_unfurl'] and 'text' in attachment:
                            # No, I don't know why this field is called `msg_subtype`.
                            quoting_format = get_quoting_format(attachment.get('msg_subtype'))
                            # No author_id if bot
                            if 'author_id' in attachment:
                                nick = self.slack_user_id_to_name[attachment['author_id']]
                            else:
                                nick = attachment['author_subname']

                            text = '\n'.join(
                                quoting_format.format(nick=nick, message=reformat_slack(line))
                                for line in attachment['text'].split('\n')
                            )

                            privmsg(irc_conn, irc_channel, text, should_reformat=False)

                # We want to post any shares before the text we're commenting on them with
                if 'text' in msg and msg['text'] != '':
                    # Is this a thread reply?
                    if 'thread_ts' in msg:
                        # Saving ourselves for the message_replied.
                        self.slack_pending_thread_reply = msg
                        return
                    # Yes, sending this inline on a connection being polled
                    # by another thread.
                    privmsg(irc_conn, irc_channel, msg['text'])

            elif subtype == 'me_message':
                irc_conn.action(irc_channel, emojize(msg['text']))
            elif subtype == 'message_replied':
                replies = sorted(msg['message']['replies'], key=lambda r: float(r['ts']))

                # This is pretty unlikely to happen, these events are usually sent back to back
                if not self.slack_pending_thread_reply:
                    # Although it will happen regularly when pshuu_mirror operates.
                    # Do a hacky thing to see if the last reply (the one we're expecting details for) was from
                    # a bot, in which case we will have just ignored because the subtype is bot_message
                    if not replies[-1]['user'].startswith('B'):
                        log.error('Got a message_replied but no pending reply, skipping: %s', json.dumps(msg, indent=4))
                        self.slack_error(f'Got a `message_replied` but no pending reply, skipping: ```{json.dumps(msg, indent=4)}```')
                    return

                # Only quotepost on the first reply or if a period of time has passed.
                if len(replies) == 1 or time.time() - float(replies[-2]['ts']) > 3600:  # 1 hour
                    # Now here is some duplication of logic.
                    # XXX: This will cause a Slack-side image upload reply to not be quoteposted
                    nick = self.slack_user_id_to_name.get(msg['message']['user']) if 'user' in msg['message'] else msg['message']['username']

                    quoting_format = get_quoting_format(msg['message'].get('subtype'))

                    # XXX: We aren't rendering attachments of the thread root. Guess that's okay...
                    if 'text' in msg['message'] and msg['message']['text'] != '':
                        if quoting_format:
                            privmsg(
                                irc_conn,
                                irc_channel,
                                quoting_format.format(
                                    nick=nick,
                                    message=reformat_slack(msg['message']['text']),
                                ),
                                should_reformat=False,
                            )

                privmsg(irc_conn, irc_channel, self.slack_pending_thread_reply['text'])
                self.slack_pending_thread_reply = None
            elif subtype == 'channel_topic':
                log.info('Channel topic changed event: %s', msg)

                # Is this a PM bridge?
                if irc_channel and irc_channel.startswith('#'):
                    return

                # Deregister old mapping if there is any
                if channel_name in self.slack_channel_to_irc:
                    log.info('Removing mapping between Slack %s and IRC %s', channel_name, irc_channel)
                    del self.slack_channel_to_irc[channel_name]
                    del self.irc_conn_to_conf[irc_conn]['channels'][irc_channel]

                # Register new mapping if any
                new_topic = msg['topic']
                if '/' in new_topic:
                    network, target = new_topic.split('/', 1)

                    if network not in self.irc_conf_name_to_conn:
                        log.error('Nonexistent network specified in Slack channel topic: %s on %s', network, channel_name)
                        return

                    log.info('Adding mapping between Slack %s and IRC %s', channel_name, target)
                    self.slack_channel_to_irc[channel_name] = (
                        self.irc_conf_name_to_conn[network],
                        target,
                    )
                    self.irc_conn_to_conf[self.irc_conf_name_to_conn[network]]['channels'][target] = channel_name

        elif type_ == 'channel_created':
            log.info('Channel created event: %s', msg)
            new_channel = msg['channel']
            self.slack_channel_name_to_id[new_channel['name']] = new_channel['id']
            self.slack_channel_id_to_name[new_channel['id']] = new_channel['name']
        elif type_ == 'channel_deleted':
            log.info('Channel deleted event: %s', msg)
            channel_name = self.slack_channel_id_to_name.pop(msg['channel'])
            del self.slack_channel_name_to_id[channel_name]

            channel_name = '#' + channel_name
            if channel_name in self.slack_channel_to_irc:
                irc_conn, _ = self.slack_channel_to_irc.pop(channel_name)
                irc_conf = self.irc_conn_to_conf[irc_conn]
                del irc_conf['channels'][
                    list(irc_conf['channels'].keys())[list(irc_conf['channels'].values()).index(channel_name)]
                ]
        elif type_ == 'goodbye':
            # XXX: Add reconnection logic
            pass
        elif type_ == 'error':
            log.error('Error from Slack stream: %s', msg)
            self.slack_error(f'Error from Slack stream: ```{msg}```')


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
    return emoji.emojize(msg, use_aliases=True)


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

    for msg in msgs:
        irc_conn.privmsg(irc_channel, msg)


if __name__ == '__main__':
    logging.basicConfig(
        format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
        level=0,
    )
    Kari()
