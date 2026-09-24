"""The deployment files have to agree with each other.

None of this runs in production, but all of it is load-bearing at deploy time, and every
assertion here corresponds to something that actually broke on a real server: a build arg
declared in the Dockerfile and never passed by compose, a data directory owned by the wrong
uid, a deploy script that referenced a path the checkout did not have.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
OBSERVABILITY = ROOT / "deploy/docker-compose.observability.yml"
DOCKERFILE = ROOT / "Dockerfile"
DEPLOY = ROOT / "deploy/deploy.sh"
BOOTSTRAP = ROOT / "deploy/bootstrap.sh"


def yaml_load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), path
    return loaded


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


class TestDeployScript:
    def test_it_deploys_the_commit_it_was_given_not_whatever_main_is(self):
        """Two quick pushes otherwise let the first deploy ship the second, untested, commit."""
        text = DEPLOY.read_text(encoding="utf-8")
        assert "SSH_ORIGINAL_COMMAND" in text
        assert "merge-base --is-ancestor" in text, "an unvalidated target is a downgrade"
        assert "^[0-9a-f]{40}$" in text

    def test_the_workflow_names_that_commit(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        assert "$GITHUB_SHA" in workflow

    def test_it_carries_the_alerts_overlay_when_one_is_configured(self):
        """Without it a push recreates Grafana from the base files and drops the alert env."""
        text = DEPLOY.read_text(encoding="utf-8")
        assert "docker-compose.alerts.yml" in text
        assert "source .env" not in text, "the forced command must not execute .env"

    def test_a_failed_start_still_reaches_the_rollback(self):
        """Under `set -e` a failing `up -d` used to end the script with nothing running."""
        text = DEPLOY.read_text(encoding="utf-8")
        assert "|| started=false" in text

    def test_deploys_are_serialised(self):
        assert "flock" in DEPLOY.read_text(encoding="utf-8")


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


class TestObservabilityConfig:
    """The dashboard and the alert rules name metrics; the code has to emit them."""

    @staticmethod
    def emitted_names() -> set[str]:
        from tests.test_metrics import source

        return {
            line.split()[0].split("{")[0]
            for line in source(supervisor=_FakeSupervisor()).render().splitlines()
            if line and not line.startswith("#")
        }

    def test_every_metric_the_dashboard_draws_is_emitted(self):
        """A panel naming a metric nobody emits draws no line, which reads as "no problems"."""
        import json

        dashboard = json.loads(
            (ROOT / "deploy/grafana/provisioning/dashboards/ddiver.json").read_text("utf-8")
        )
        used = set()
        for panel in dashboard["panels"]:
            for target in panel.get("targets", []):
                used.update(re.findall(r"\bddiver_[a-z_]+\b", target.get("expr", "")))
        assert used - self.emitted_names() == set()

    def test_every_metric_the_alerts_use_is_emitted(self):
        rules = yaml_load(ROOT / "deploy/grafana/alerting/rules.yml")
        used = set()
        for group in rules["groups"]:
            for rule in group["rules"]:
                for query in rule["data"]:
                    used.update(re.findall(r"\bddiver_[a-z_]+\b", query["model"]["expr"]))
        assert used - self.emitted_names() == set()

    def test_no_rule_alerts_on_missing_data(self):
        """The default is NoData, which fires. Every rule here is empty on a healthy system,
        so the defaults page you five times a day to say the session is invalid when it is not.
        """
        rules = yaml_load(ROOT / "deploy/grafana/alerting/rules.yml")
        for group in rules["groups"]:
            for rule in group["rules"]:
                assert rule.get("noDataState") == "OK", rule["uid"]
                assert rule.get("execErrState") == "Error", rule["uid"]

    def test_no_rule_compares_a_gauge_to_zero_with_equals(self):
        """`x == 0` is a filter returning 0, and Grafana reads 0 as Normal: it never fires."""
        rules = yaml_load(ROOT / "deploy/grafana/alerting/rules.yml")
        for group in rules["groups"]:
            for rule in group["rules"]:
                for query in rule["data"]:
                    assert "== 0" not in query["model"]["expr"], rule["uid"]


class _SupervisorState:
    degraded = False


class _FakeSupervisor:
    """Enough of a supervisor for MetricsSource to emit ddiver_degraded."""

    state = _SupervisorState()
