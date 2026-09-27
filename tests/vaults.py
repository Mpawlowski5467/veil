"""Vault test doubles shared by several test modules."""


class DictVault:
    """A third-party vault: implements Vault, but can't remember values."""

    def __init__(self):
        self._by_value = {}
        self._by_placeholder = {}
        self._counters = {}

    def get_or_create(self, value, entity_type):
        if value in self._by_value:
            return self._by_value[value]
        number = self._counters.get(entity_type, 0) + 1
        self._counters[entity_type] = number
        placeholder = f"[{entity_type}_{number}]"
        self._by_value[value] = placeholder
        self._by_placeholder[placeholder] = value
        return placeholder

    def get_placeholder(self, value):
        return self._by_value.get(value)

    def get_value(self, placeholder):
        return self._by_placeholder.get(placeholder)

    def items(self):
        return list(self._by_placeholder.items())

    def clear(self):
        self._by_value.clear()
        self._by_placeholder.clear()
        self._counters.clear()

    def __len__(self):
        return len(self._by_value)
