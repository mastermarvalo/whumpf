"""Tests for check_env_passthrough.py (stdlib only).

    python3 -m unittest discover -s scripts -p "test_check_env*.py"
"""

from __future__ import annotations

import unittest

import check_env_passthrough as check

COMPOSE = """\
services:
  postgis:
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-whumpf}

  api:
    image: whumpf-api:dev
    environment:
      # a comment
      WHUMPF_ENV: ${WHUMPF_ENV:-dev}
      TTL_S: ${TTL_S:-86400}
      FLAG: ${FLAG:-false}
    volumes:
      - ./backend/app:/app/app

  frontend:
    environment:
      VITE_API_URL: ${VITE_API_URL:-http://localhost:8000}
"""

CONFIG = """\
class Settings(BaseSettings):
    whumpf_env: str = "dev"
    ttl_s: int = 24 * 3600
    flag: bool = False
"""

EXAMPLE = """\
POSTGRES_DB=whumpf
# WHUMPF_ENV=prod
VITE_API_URL=http://localhost:8000
BACKUP_S3_BUCKET=
"""


class CheckEnvTest(unittest.TestCase):
    def test_everything_passed_through_is_ok(self):
        self.assertEqual(check.problems(COMPOSE, EXAMPLE, CONFIG), [])

    def test_example_variable_not_in_compose(self):
        found = check.problems(COMPOSE, EXAMPLE + "# NPS_API_KEY=\n", CONFIG)
        self.assertEqual(len(found), 1)
        self.assertIn("NPS_API_KEY is in .env.example", found[0])

    def test_setting_missing_from_the_api_service(self):
        # Referenced by another service only: still not reaching the api.
        config = CONFIG + '    vite_api_url: str = ""\n'
        found = check.problems(COMPOSE, EXAMPLE, config)
        self.assertEqual(found, ["VITE_API_URL is a Settings field but not in the api service's environment"])

    def test_default_must_match_config(self):
        found = check.problems(COMPOSE.replace("FLAG:-false", "FLAG:-true"), EXAMPLE, CONFIG)
        self.assertEqual(found, ["FLAG: compose.yml defaults to 'true', config.py to 'false'"])

    def test_environment_is_read_from_the_api_service_only(self):
        env = check.service_environment(COMPOSE, "api")
        self.assertEqual(set(env), {"WHUMPF_ENV", "TTL_S", "FLAG"})


if __name__ == "__main__":
    unittest.main()
