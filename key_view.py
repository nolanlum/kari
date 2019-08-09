from collections import UserList
from operator import attrgetter
from operator import itemgetter


class KeyViewList(UserList):

    class KeyView(dict):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.backing = None
            self.key = None

        @classmethod
        def create(cls, backing, key):
            obj = cls((key(item), item) for item in backing.data)
            obj.backing = backing
            obj.key = key
            return obj

        def add(self, item):
            self[self.key(item)] = item

        def remove(self, item):
            del self[self.key(item)]

        def extend(self, items):
            for item in items:
                self.add(item)

        def refresh(self):
            self.clear()
            self.extend(self.backing)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.views = []

    def register(self, key):
        view = KeyViewList.KeyView.create(backing=self, key=key)
        self.views.append(view)
        return view

    def register_itemgetter(self, item):
        return self.register(key=itemgetter(item))

    def register_attrgetter(self, item):
        return self.register(key=attrgetter(item))

    def refresh(self):
        # Ideally, we wouldn't need this...
        for view in self.views:
            view.clear()
            view.extend(self.data)

    def __setitem__(self, i, item):
        old_item = self.data[i]
        self.data[i] = item

        for view in self.views:
            view.remove(old_item)
            view.add(item)

    def __delitem__(self, i):
        item = self.data[i]
        del self.data[i]

        for view in self.views:
            view.remove(item)

    def append(self, item):
        if item in self.data:
            raise ValueError("Duplicate items are not supported")

        self.data.append(item)

        for view in self.views:
            view.add(item)

    def insert(self, i, item):
        if item in self.data:
            raise ValueError("Duplicate items are not supported")

        self.data.insert(i, item)

        for view in self.views:
            view.add(item)

    def pop(self, i=-1):
        item = self.data.pop(i)

        for view in self.views:
            view.remove(item)

    def remove(self, item):
        self.data.remove(item)

        for view in self.views:
            view.remove(item)

    def clear(self):
        self.data.clear()

        for view in self.views:
            view.clear()

    def extend(self, other):
        super().extend(other)

        for view in self.views:
            view.extend(other)
