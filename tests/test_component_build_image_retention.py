from __future__ import annotations

import subprocess

import pytest

from scripts.component_build_isolation import BUILD_IMAGE_KEEP as KEEP, IsolationError, ProducerSession, prune_build_images

TOTAL = KEEP + 3
REPOS = {"source": "wuwaterm-component-build", "wheel": "wuwaterm-wheel-build"}


def _ref(kind: str, n: int) -> str:
    return f"{REPOS[kind]}:2026110{n}T010203Z-{n:032x}"


def _images(kind: str, count: int) -> dict[str, str]:
    return {_ref(kind, n): f"sha256:{n:064x}" for n in range(1, count + 1)}


class FakeDocker:
    """Tiny Docker engine: reference -> image id, ids that containers use, and one step to refuse."""

    def __init__(self, images: dict[str, str], containers: tuple[str, ...] = (), refuse: str = "") -> None:
        self.images, self.containers, self.refuse, self.calls = dict(images), set(containers), refuse, []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(argv)
        step, ref, code, out = argv[0] + argv[1], argv[-1], 1, b""
        if step == self.refuse:
            pass
        elif step == "imagels":
            prefix = argv[3].removeprefix("reference=") + ":"
            code, out = 0, "".join(r.removeprefix(prefix) + "\n" for r in self.images if r.startswith(prefix)).encode()
        elif step == "imageinspect" and ref in self.images:
            code, out = 0, self.images[ref].encode()
        elif step == "ps-a":
            code, out = 0, b"c\n" if ref.removeprefix("ancestor=") in self.containers else b""
        elif step == "imagerm" and ref in self.images:
            if list(self.images.values()).count(self.images[ref]) > 1 or self.images[ref] not in self.containers:
                code = 0  # Docker lets an in-use image lose one of several tags, never its last
                del self.images[ref]
        return subprocess.CompletedProcess(argv, code, out, b"")


@pytest.mark.parametrize("kind", ["source", "wheel"])
def test_keeps_newest_and_leaves_other_tags_and_repositories(kind: str) -> None:
    foreign = {REPOS[kind] + ":latest": "sha256:" + "f" * 64, _ref("wheel" if kind == "source" else "source", 1): "e"}
    docker = FakeDocker({**_images(kind, TOTAL), **foreign})
    assert sorted(prune_build_images(kind, _ref(kind, TOTAL), docker)) == [_ref(kind, n) for n in (1, 2, 3)]
    assert set(docker.images) == {_ref(kind, n) for n in range(4, TOTAL + 1)} | set(foreign)
    assert not any(arg in ("-f", "--force") for argv in docker.calls for arg in argv)


def test_always_keeps_the_just_built_reference() -> None:
    docker = FakeDocker(_images("source", TOTAL))
    prune_build_images("source", _ref("source", 1), docker)
    assert set(docker.images) == {_ref("source", n) for n in (1, *range(4, TOTAL + 1))}


def test_skips_an_older_image_that_a_stopped_container_uses() -> None:
    images = {**_images("wheel", TOTAL), "wuwaterm-runtime:serving": _images("wheel", 2)[_ref("wheel", 2)]}
    docker = FakeDocker(images, containers=(images[_ref("wheel", 2)],))
    assert sorted(prune_build_images("wheel", _ref("wheel", TOTAL), docker)) == [_ref("wheel", n) for n in (1, 3)]
    assert _ref("wheel", 2) in docker.images


def test_docker_failures_leave_images_and_never_raise() -> None:
    for broken in (IsolationError("no context"), subprocess.TimeoutExpired("docker", 60), OSError("gone")):
        def explode(argv: list[str], broken: Exception = broken) -> subprocess.CompletedProcess[bytes]:
            raise broken
        assert prune_build_images("source", _ref("source", TOTAL), explode) == []
    assert prune_build_images("unknown", _ref("source", TOTAL), FakeDocker(_images("source", TOTAL))) == []
    for step in ("imagels", "imageinspect", "ps-a", "imagerm"):
        docker = FakeDocker(_images("source", TOTAL), refuse=step)
        assert prune_build_images("source", _ref("source", TOTAL), docker) == []
        assert docker.images == _images("source", TOTAL)


def test_session_method_uses_its_docker_transport_and_swallows_failures() -> None:
    session = object.__new__(ProducerSession)
    session.ledger = {"kind": "wheel", "retained_reference": {"ref": _ref("wheel", TOTAL)}}
    docker, timeouts = FakeDocker(_images("wheel", TOTAL - 1)), []
    session.read_docker = lambda argv, timeout=30: (timeouts.append(timeout), docker(argv))[1]
    assert sorted(session.prune_build_images()) == [_ref("wheel", n) for n in (1, 2)]
    assert set(timeouts) == {60}
    session.read_docker = lambda argv, timeout=30: (_ for _ in ()).throw(IsolationError("cannot inspect Docker state"))
    assert session.prune_build_images() == []
