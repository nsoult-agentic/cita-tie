"""No-op speaker stub for headless Docker environments."""


class Speaker:
    def say(self, text):
        pass


def new_speaker():
    return Speaker()
