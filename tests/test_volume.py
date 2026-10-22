import pytest

from wqs import volume


class FakeEndpoint:
    def __init__(self, level=0.5, muted=False):
        self.level, self.muted = level, muted
        self.opened = self.closed = 0

    def __call__(self):  # the factory
        return self

    def __enter__(self):
        self.opened += 1
        return self

    def __exit__(self, *a):
        self.closed += 1

    def get_level(self):
        return self.level

    def set_level(self, level):
        self.level = level

    def get_mute(self):
        return self.muted

    def set_mute(self, muted):
        self.muted = muted


def test_get_volume_scales_to_percent():
    ep = FakeEndpoint(0.427, True)
    assert volume.get_volume(ep) == {"level": 43, "muted": True}
    assert ep.opened == ep.closed == 1


@pytest.mark.parametrize("value,expected", [(30, 0.30), (-1, 0.0), (130, 1.0)])
def test_set_volume_clamps(value, expected):
    ep = FakeEndpoint()
    volume.set_volume(value, ep)
    assert ep.level == pytest.approx(expected)
    assert ep.closed == 1


def test_set_mute():
    ep = FakeEndpoint()
    volume.set_mute(True, ep)
    assert ep.muted is True
    volume.set_mute(0, ep)
    assert ep.muted is False


def test_guid_bytes_are_com_layout():
    g = volume._guid(volume.IID_IAudioEndpointVolume)
    # Data1 is little endian in memory.
    assert bytes(g.data)[:4] == bytes.fromhex("822cdf5c")
