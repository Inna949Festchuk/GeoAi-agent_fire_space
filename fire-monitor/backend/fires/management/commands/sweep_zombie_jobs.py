"""
Finalize jobs stuck in 'running' as 'failed'.

A job whose process died (backend OOM-killed or restarted mid-run) never
gets its status updated, so it stays 'running' forever: the frontend keeps
showing a live-progress banner for a dead run, and the burn mapping
duplicate-run guard treats it as a live run. This command marks such jobs
failed based on how long they have been running.

Usage:
    python manage.py sweep_zombie_jobs
    python manage.py sweep_zombie_jobs --timeout-minutes 60
    python manage.py sweep_zombie_jobs --job-type burn_mapping --dry-run
"""

import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from geo_processing.burn_mapping import find_zombie_jobs, sweep_zombie_jobs

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Mark stuck "running" processing jobs as failed (zombie cleanup after OOM/restart)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout-minutes',
            type=int,
            default=None,
            help='Jobs running longer than this are marked failed '
                 f'(default: JOB_TIMEOUT_MINUTES={settings.JOB_TIMEOUT_MINUTES})',
        )
        parser.add_argument(
            '--job-type',
            type=str,
            default=None,
            choices=[None, 'fire_detection', 'burn_mapping'],
            help='Only sweep this job type (default: all types)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report which jobs would be finalized without changing them',
        )

    def handle(self, *args, **options):
        timeout = options['timeout_minutes'] or settings.JOB_TIMEOUT_MINUTES

        if options['dry_run']:
            jobs = list(find_zombie_jobs(timeout, options['job_type']))
            if not jobs:
                self.stdout.write(self.style.SUCCESS(
                    f'No zombie jobs (running over {timeout} minutes).'
                ))
                return
            for job in jobs:
                self.stdout.write(
                    f'  would mark job {job.id} ({job.job_type}, '
                    f'started {job.started_at}) as failed'
                )
            self.stdout.write(f'{len(jobs)} job(s) would be finalized.')
            return

        count = sweep_zombie_jobs(timeout_minutes=timeout, job_type=options['job_type'])
        if count:
            self.stdout.write(self.style.WARNING(
                f'Marked {count} zombie job(s) as failed.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'No zombie jobs (running over {timeout} minutes).'
            ))
