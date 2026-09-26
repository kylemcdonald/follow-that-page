import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import botlib
from follow_that_page import ChangeResult


class FakeResponses:
    def __init__(self, output_text):
        self.output_text = output_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class FakeOpenAIClient:
    def __init__(self, output_text):
        self.responses = FakeResponses(output_text)


class RobustModeTests(unittest.TestCase):
    def test_robust_assessment_uses_structured_output(self):
        client = FakeOpenAIClient(
            '{"notify": true, "summary": "A refurbished MacBook Air was added."}'
        )

        assessment = botlib.assess_robust_change(
            client=client,
            url="https://example.com",
            selector="body",
            criteria="Only notify on inventory changes.",
            diff_text="- no listings\n+ one listing",
        )

        self.assertTrue(assessment.notify)
        self.assertEqual(assessment.summary, "A refurbished MacBook Air was added.")
        request = client.responses.calls[0]
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["reasoning"], {"effort": "minimal"})
        self.assertEqual(request["text"]["format"]["type"], "json_schema")

    def test_robust_mode_suppresses_an_irrelevant_diff(self):
        client = FakeOpenAIClient('{"notify": false, "summary": ""}')
        change = ChangeResult(
            previous_state_label="HTTP 200",
            current_state_label="HTTP 200",
            diff_text="- analytics token A\n+ analytics token B",
            selected_html="<body></body>",
        )

        with TemporaryDirectory() as temporary_directory:
            jobs_path = Path(temporary_directory) / "jobs.json"
            jobs_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "apple",
                            "url": "https://example.com",
                            "selector": "body",
                            "mode": "robust",
                            "change_criteria": "Only notify on inventory changes.",
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with patch.object(
                botlib, "check_page_for_changes", return_value=change
            ), patch.object(botlib, "send_telegram_message") as send_message:
                notification_count = botlib.monitor_jobs_once(
                    openai_client=client,
                    bot_token="token",
                    chat_id="chat",
                    jobs_path=jobs_path,
                )

        self.assertEqual(notification_count, 0)
        send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
