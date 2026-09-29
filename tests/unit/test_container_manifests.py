from pathlib import Path

import yaml


def test_dockerfile_exists_and_uses_non_root():
    df_path = Path("docker/Dockerfile.orchestrator")
    assert df_path.exists(), "docker/Dockerfile.orchestrator must exist"
    df = df_path.read_text(encoding="utf-8")
    assert "USER 10001:10001" in df
    assert "python:3.12.2-slim-bookworm" in df
    assert "EXPOSE 8000" in df

def test_hpa_manifest():
    hpa_path = Path("k8s/hpa/warden-orchestrator-hpa.yaml")
    assert hpa_path.exists(), "k8s/hpa/warden-orchestrator-hpa.yaml must exist"
    hpa = yaml.safe_load(hpa_path.read_text(encoding="utf-8"))
    assert hpa["apiVersion"] == "autoscaling/v2"
    assert hpa["kind"] == "HorizontalPodAutoscaler"
    assert hpa["spec"]["minReplicas"] == 2
    assert hpa["spec"]["maxReplicas"] == 6
    assert hpa["spec"]["scaleTargetRef"]["name"] == "warden-orchestrator"
