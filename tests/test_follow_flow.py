import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import botlib
import telegram_daemon as daemon
from test_botlib import FakeOpenAIClient


class FollowFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        original = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, original)
        self.send = patch.object(daemon, 'send_telegram_message').start()
        patch.object(daemon, 'validate_follow_target').start()
        patch.object(daemon, 'answer_callback_query').start()
        self.addCleanup(patch.stopall)

    def begin(self):
        daemon.handle_follow('token', '123', '/follow https://example.com')
        self.assertEqual(botlib.load_jobs(), [])
        return daemon.load_runtime_config()['pending_follow']['id']

    def choose(self, request_id, mode, username='owner'):
        daemon.handle_callback_query(
            bot_token='token', chat_id='123', owner_username='owner',
            callback_query={'id': 'callback', 'from': {'username': username},
                            'message': {'chat': {'id': 123}},
                            'data': f'follow:{request_id}:{mode}'})

    def test_standard_choice_is_saved_and_old_button_is_ignored(self):
        request_id = self.begin()
        self.choose(request_id, 'standard')
        self.assertEqual(botlib.load_jobs()[0].mode, 'standard')
        self.choose(request_id, 'robust')
        self.assertNotIn('pending_follow', daemon.load_runtime_config())

    def test_robust_waits_for_criteria_and_persists_them(self):
        self.choose(self.begin(), 'robust')
        self.assertEqual(botlib.load_jobs(), [])
        daemon.handle_message(bot_token='token', chat_id='123', owner_username='owner',
                              message={'from': {'username': 'owner'},
                                       'chat': {'id': 123}, 'text': 'Only price changes'})
        job = botlib.load_jobs()[0]
        self.assertEqual((job.mode, job.change_criteria), ('robust', 'Only price changes'))
        self.assertNotIn('pending_follow', daemon.load_runtime_config())

    def test_text_choice_is_offered_saved_and_listed_without_criteria(self):
        request_id = self.begin()
        buttons = self.send.call_args.kwargs['reply_markup']['inline_keyboard']
        self.assertTrue(any(button['callback_data'] == f'follow:{request_id}:text'
                            for row in buttons for button in row))
        self.choose(request_id, 'text')
        job = botlib.load_jobs()[0]
        self.assertEqual(job.mode, 'text')
        self.assertIsNone(job.change_criteria)
        self.assertNotIn('pending_follow', daemon.load_runtime_config())
        self.assertIn('(text only)', daemon.build_jobs_reply()[0])

    def test_unauthorized_choice_does_not_save_job(self):
        self.choose(self.begin(), 'standard', username='stranger')
        self.assertEqual(botlib.load_jobs(), [])

    def test_repeated_follow_updates_saved_mode(self):
        botlib.add_job('https://example.com', 'body', mode='robust', change_criteria='Price')
        botlib.add_job('https://example.com', 'body', mode='standard')
        jobs = botlib.load_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].mode, 'standard')
        self.assertIsNone(jobs[0].change_criteria)

    @patch.dict(os.environ, {'OPENAI_MODEL': '', 'OPENAI_REASONING_EFFORT': ''})
    def test_summary_uses_requested_defaults(self):
        client = FakeOpenAIClient('Price changed.')
        botlib.summarize_diff_with_openai(client, 'https://example.com', 'body', '- 1\n+ 2')
        request = client.responses.calls[0]
        self.assertEqual(request['model'], 'gpt-6-luna')
        self.assertEqual(request['reasoning'], {'effort': 'minimal'})
