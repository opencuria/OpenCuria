import pytest
import uuid
from datetime import timedelta
from django.utils import timezone
from apps.runners.models import Runner, RunnerSystemMetrics
from apps.runners.repositories import RunnerSystemMetricsRepository, RunnerRepository
from apps.organizations.models import Organization
from common.utils import hash_token

@pytest.mark.django_db
class TestRunnerSystemMetricsRepository:
    def test_get_history(self):
        org = Organization.objects.create(name="Test Org")
        runner = Runner.objects.create(
            name="test-runner",
            api_token_hash=hash_token("token"),
            organization=org
        )
        
        now = timezone.now()
        
        # Create metrics at different times
        # 2 hours ago
        m1 = RunnerSystemMetrics.objects.create(
            runner=runner,
            timestamp=now - timedelta(hours=2),
            cpu_usage_percent=10.0,
            ram_used_bytes=1000,
            ram_total_bytes=2000,
            disk_used_bytes=1000,
            disk_total_bytes=2000
        )
        
        # 1 hour ago
        m2 = RunnerSystemMetrics.objects.create(
            runner=runner,
            timestamp=now - timedelta(hours=1),
            cpu_usage_percent=20.0,
            ram_used_bytes=1000,
            ram_total_bytes=2000,
            disk_used_bytes=1000,
            disk_total_bytes=2000,
            vm_metrics={
                str(uuid.uuid4()): {
                    "cpu_usage_percent": 84.2,
                    "ram_used_bytes": 1200,
                    "ram_total_bytes": 2000,
                    "disk_used_bytes": 1800,
                    "disk_total_bytes": 2000,
                }
            },
        )
        
        # 25 hours ago (should be excluded if we ask for 24h)
        m3 = RunnerSystemMetrics.objects.create(
            runner=runner,
            timestamp=now - timedelta(hours=25),
            cpu_usage_percent=30.0,
            ram_used_bytes=1000,
            ram_total_bytes=2000,
            disk_used_bytes=1000,
            disk_total_bytes=2000
        )
        
        # Test get_history for last 24h
        since = now - timedelta(hours=24)
        history = list(RunnerSystemMetricsRepository.get_history(runner.id, since))
        
        assert len(history) == 2
        assert m1 in history
        assert m2 in history
        assert m3 not in history
        
        # Check ordering (oldest first)
        assert history[0].timestamp < history[1].timestamp
        assert history[1].vm_metrics is not None
