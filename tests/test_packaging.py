"""The deployment files have to agree with each other.

None of this runs in production, but all of it is load-bearing at deploy time, and every
assertion here corresponds to something that actually broke on a real server: a build arg
declared in the Dockerfile and never passed by compose, a data directory owned by the wrong
uid, a deploy script that referenced a path the checkout did not have.
"""

from __future__ import annotations

import re
import subprocess
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

    def test_it_renders_the_alerting_files_before_starting(self):
        """They go into the directory Grafana already mounts, so they must exist by then.

        A second bind mount nested inside the first is what this replaced: Docker refuses to
        create the child mountpoint because the parent is mounted read-only, so with alerts
        configured the whole stack failed to start — and the rollback, reusing the same file
        list, failed the same way.
        """
        text = DEPLOY.read_text(encoding="utf-8")
        assert "render-alerts.sh" in text
        assert "docker-compose.alerts.yml" not in text
        assert "source .env" not in text, "the forced command must not execute .env"

    def test_it_notices_when_it_has_updated_itself(self):
        """SSH starts this script from the checkout as it was before the fetch.

        `git reset --hard` then replaces the file through a rename, so the open copy keeps
        running and a change to the script lands one deploy late. That cost a fix silently:
        the `host-metrics` profile was in the commit, the deploy reported success, and
        node-exporter was still not running.
        """
        text = DEPLOY.read_text(encoding="utf-8")
        assert 'running="$(cat "$0")"' in text
        assert '[[ "$(cat "$0")" != "$running" ]]' in text

    def test_a_bad_alert_setting_does_not_kill_the_deploy(self):
        """render-alerts.sh rejects a malformed ALERT_TELEGRAM_* value with exit 1.

        It runs after the reset and after the build, so under `set -e` a stray quote in a
        notification setting left the box running the old container on a new tree, with no
        `up -d` and no rollback.
        """
        text = DEPLOY.read_text(encoding="utf-8")
        assert 'if ! "$APP_DIR/deploy/render-alerts.sh"; then' in text

    def test_a_failed_start_still_reaches_the_rollback(self):
        """Under `set -e` a failing `up -d` used to end the script with nothing running."""
        text = DEPLOY.read_text(encoding="utf-8")
        assert "|| started=false" in text

    def test_deploys_are_serialised(self):
        assert "flock" in DEPLOY.read_text(encoding="utf-8")


def _bash_function(name: str, script: Path) -> str:
    """Pull one shell function out of a script, so a test can run it on its own."""
    text = script.read_text(encoding="utf-8")
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


class TestDeployScriptActuallyRuns:
    """The only tests here that execute part of the script instead of reading it.

    `_alerting_config` used to be a pipeline ending in `shasum`, and with alerting off there
    are no files to read: the glob stayed literal, `cat` exited 1, `pipefail` handed that to
    the assignment and `set -e` ended the deploy — after the checkout had moved and the image
    had been built, with no output, no `up -d` and no rollback. Alerting off is the documented
    default, so that was every box nobody had configured it on. Twenty-three tests read this
    file and none of them ran a line of it; grepping for a guard would not have caught it.
    """

    def _fingerprint(self, app_dir: Path) -> subprocess.CompletedProcess[str]:
        body = _bash_function("_alerting_config", DEPLOY)
        # The assignment, not just the call: `set -e` acts on the status of a command, and
        # `printf "%s" "$(f)"` is printf's status, not f's. Writing it the other way is how
        # the first version of this test passed against the very bug it was written for.
        script = (
            f'set -Eeuo pipefail\nAPP_DIR="$1"\n{body}\n'
            'before="$(_alerting_config)"\nprintf "%s" "$before"\n'
        )
        return subprocess.run(  # noqa: S603 - the script is this repository's own file
            ["bash", "-c", script, "bash", str(app_dir)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )

    @staticmethod
    def _tree(tmp_path: Path) -> Path:
        (tmp_path / "deploy/grafana/provisioning/alerting").mkdir(parents=True)
        return tmp_path

    def test_it_survives_a_box_with_alerting_off(self, tmp_path):
        app = self._tree(tmp_path)
        result = self._fingerprint(app)
        assert result.returncode == 0, f"the deploy would have died here: {result.stderr}"

    def test_it_survives_a_provisioning_directory_that_does_not_exist_at_all(self, tmp_path):
        result = self._fingerprint(tmp_path)
        assert result.returncode == 0, result.stderr

    def test_it_still_notices_a_change(self, tmp_path):
        """Swallowing the failure must not swallow the comparison it exists for."""
        app = self._tree(tmp_path)
        off = self._fingerprint(app)
        (app / "deploy/grafana/provisioning/alerting/rules.yml").write_text("groups: []\n")
        on = self._fingerprint(app)
        assert on.returncode == 0, on.stderr
        assert on.stdout != off.stdout


class TestAlerting:
    def test_nothing_mounts_inside_the_provisioning_mount(self):
        """Docker cannot create a mountpoint inside a read-only bind, so the stack would not
        start at all — the failure this restructuring removed."""
        for path in (COMPOSE, OBSERVABILITY):
            for service in yaml_load(path)["services"].values():
                targets = [
                    str(v).split(":")[1] for v in service.get("volumes", []) if ":" in str(v)
                ]
                nested = [
                    t
                    for t in targets
                    if any(
                        t != other and t.startswith(other.rstrip("/") + "/") for other in targets
                    )
                ]
                assert nested == [], nested

    def test_every_scrape_target_is_something_the_deploy_starts(self):
        """A target nobody starts is a red row in Prometheus forever, and an empty panel.

        node-exporter is behind the `host-metrics` profile because on Docker Desktop it cannot
        mount `/` and would be measuring a Linux VM rather than the Mac. Nothing turned that
        profile on for the box where both of those stop being true, so the Host panel on the
        real server was empty and the `node` job was permanently down.
        """
        prometheus = yaml_load(ROOT / "deploy/prometheus.yml")
        compose = yaml_load(OBSERVABILITY)
        deploy = DEPLOY.read_text(encoding="utf-8")
        services = compose["services"]
        for job in prometheus["scrape_configs"]:
            for target in job["static_configs"][0]["targets"]:
                host = target.split(":")[0]
                if host in {"localhost", "ddiver"}:
                    continue  # prometheus itself, and the app from the other compose file
                assert host in services, f"{job['job_name']} scrapes {host}, which is not a service"
                for profile in services[host].get("profiles", []):
                    assert profile in deploy, (
                        f"{host} is only started with the '{profile}' profile,"
                        " which deploy.sh does not enable"
                    )

    def test_the_rendered_directory_is_tracked_but_its_contents_are_not(self):
        """git does not track empty directories, and deploy.sh does `git reset --hard`."""
        assert (ROOT / "deploy/grafana/provisioning/alerting/.gitkeep").exists()
        ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "deploy/grafana/provisioning/alerting/*.yml" in ignored

    def test_the_chat_id_is_rendered_and_the_token_is_not(self):
        """Two different problems, two different answers.

        Grafana expands $VARS and then re-types the result, so an all-digit chat id arrives as
        a JSON number and the contact point is rejected at boot — it has to be a rendered
        literal. A token is not all-digits and survives expansion, so it stays a variable,
        which keeps every secret out of a file that Grafana's own uid has to be able to read.
        """
        tmpl = (ROOT / "deploy/grafana/alerting/contact-points.yml.tmpl").read_text("utf-8")
        body = "\n".join(x for x in tmpl.splitlines() if not x.lstrip().startswith("#"))
        assert '"@@ALERT_TELEGRAM_CHAT_ID@@"' in body, "the chat id must be rendered, quoted"
        assert "$ALERT_TELEGRAM_CHAT_ID" not in body, "and never left for Grafana to re-type"
        assert "$ALERT_TELEGRAM_TOKEN" in body, "the token must not be written to disk"
        assert "@@ALERT_TELEGRAM_TOKEN@@" not in body

    def test_the_renderer_leaves_the_files_readable_by_grafana(self):
        """Grafana runs as its own uid and reads these over a bind mount, where the numeric
        owner is what counts: 0600 owned by the deploy user makes it exit 1 and restart
        forever, and deploy.sh reports success because it only watches the ddiver container.
        """
        script = (ROOT / "deploy/render-alerts.sh").read_text(encoding="utf-8")
        assert "install -m 600" not in script
        assert "chmod 644" in script or "install -m 644" in script

    def test_grafana_is_given_the_alert_token(self):
        grafana = yaml_load(OBSERVABILITY)["services"]["grafana"]
        assert "ALERT_TELEGRAM_TOKEN" in grafana["environment"]


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
            for line in source(supervisor=_FakeSupervisor(), watchdog=_FakeWatchdog())
            .render()
            .splitlines()
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

    def test_no_rule_can_evaluate_to_zero_when_the_fault_is_present(self):
        """A PromQL comparison is a filter: it keeps the sample's own value.

        `x == 0` and `x < 1` both return a sample whose value is 0 when the fault is present,
        and Grafana reads 0 as Normal — so neither ever fires. Only `bool` turns a comparison
        into the 1 a condition needs. Comparisons whose true case is already non-zero
        (`== 1`, `> 500`, `> 0`) are fine.
        """
        suspicious = ("== 0", "< 1", "<= 0", "!= 1")
        rules = yaml_load(ROOT / "deploy/grafana/alerting/rules.yml")
        for group in rules["groups"]:
            for rule in group["rules"]:
                for query in rule["data"]:
                    expr = " ".join(query["model"]["expr"].split())
                    # `and` and `unless` return the left-hand side's samples, so only the
                    # first operand decides the value the condition sees. A `== 0` used as a
                    # filter on the right is fine, and is how "failing and nothing arriving"
                    # is written.
                    decides = re.split(r"\s+(?:and|unless|or)\s+", expr)[0]
                    if any(token in decides for token in suspicious):
                        assert "bool" in decides, f"{rule['uid']}: {decides}"


class _SupervisorState:
    degraded = False


class _FakeSupervisor:
    """Enough of a supervisor for MetricsSource to emit ddiver_degraded."""

    state = _SupervisorState()


class _FakeWatchdog:
    """Enough of a watchdog for MetricsSource to emit the dead-man's switch counters."""

    enabled = True
    sent = 0
    failed = 0
    skipped = 0
