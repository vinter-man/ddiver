"""The deployment files have to agree with each other.

None of this runs in production, but all of it is load-bearing at deploy time, and every
assertion here corresponds to something that actually broke on a real server: a build arg
declared in the Dockerfile and never passed by compose, a data directory owned by the wrong
uid, a deploy script that referenced a path the checkout did not have.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
OBSERVABILITY = ROOT / "deploy/docker-compose.observability.yml"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY = ROOT / "deploy/deploy.sh"
BOOTSTRAP = ROOT / "deploy/bootstrap.sh"


def yaml_load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestBuildArg:
    def test_the_dockerfile_takes_the_commit_and_keeps_it(self):
        text = DOCKERFILE.read_text(encoding="utf-8")
        assert "ARG GIT_SHA" in text
        assert "DDIVER_COMMIT=$GIT_SHA" in text

    def test_compose_actually_passes_it(self):
        """Declaring the ARG and never passing it leaves every image saying `unknown`."""
        build = yaml_load(COMPOSE)["services"]["ddiver"]["build"]
        assert isinstance(build, dict), "build: . cannot carry args"
        assert "GIT_SHA" in build.get("args", {})

    def test_deploy_sets_it_on_both_paths(self):
        """Including the rollback, which builds too — an unlabelled rollback is a mystery."""
        text = DEPLOY.read_text(encoding="utf-8")
        builds = [line for line in text.splitlines() if "compose" in line and "build" in line]
        assert builds, "deploy.sh no longer builds anything"
        for line in builds:
            assert "GIT_SHA=" in line, line.strip()


class TestIsolation:
    def test_the_compose_project_is_named(self):
        """Without it the project name comes from the directory, and two checkouts collide."""
        assert yaml_load(COMPOSE).get("name") == "ddiver"

    def test_every_service_has_a_memory_limit(self):
        """One runaway must not take a box that has other projects on it."""
        for path in (COMPOSE, OBSERVABILITY):
            for name, service in yaml_load(path)["services"].items():
                if "image" not in service and "build" not in service:
                    continue  # an override fragment, not a service definition
                assert service.get("mem_limit"), f"{path.name}:{name} has no mem_limit"

    def test_nothing_listens_beyond_localhost(self):
        """The bot needs no inbound connection, and the dashboards are for the tunnel."""
        for path in (COMPOSE, OBSERVABILITY):
            for name, service in yaml_load(path)["services"].items():
                for published in service.get("ports", []):
                    assert str(published).startswith("127.0.0.1:"), f"{path.name}:{name}"


class TestBootstrap:
    def test_data_is_owned_by_the_uid_the_container_runs_as(self):
        """A bind mount cares about the number, not the name: the two must be kept in step."""
        uid = re.search(r"useradd .*--uid (\d+) ddiver", DOCKERFILE.read_text(encoding="utf-8"))
        assert uid, "the Dockerfile no longer pins a uid"
        assert f"CONTAINER_UID={uid.group(1)}" in BOOTSTRAP.read_text(encoding="utf-8")

    def test_it_never_prints_a_private_key(self):
        """It used to, which writes a key to the machine into whatever log is watching."""
        text = BOOTSTRAP.read_text(encoding="utf-8")
        assert 'cat "$CI_KEY"' not in text
        assert '$(cat "$CI_KEY")' not in text

    def test_re_running_it_brings_an_existing_checkout_up_to_date(self):
        """The forced command runs a script from the checkout; a stale one cannot fix itself."""
        text = BOOTSTRAP.read_text(encoding="utf-8")
        assert "reset --hard --quiet origin/main" in text
