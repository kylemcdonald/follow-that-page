import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import botlib
import follow_that_page as page
from test_botlib import FakeOpenAIClient


class TextModeTests(unittest.TestCase):
    def test_ignores_scripts_attributes_comments_and_whitespace(self):
        before = '''<body><script>window.inventory = {token: "old"}</script>
            <style>.a {color: red}</style><template>old</template>
            <noscript>old</noscript><!-- old -->
            <a href="/product?fnode=old" class="old">MacBook&nbsp;Air</a>
            <p>$1,779.00</p></body>'''
        after = '''<body><script>window.inventory = {token: "new"}</script>
            <style>.a {color: blue}</style><template>new</template>
            <noscript>new</noscript><!-- new -->
            <a href="/product?fnode=new" class="new"><b>MacBook</b> Air</a>
            <div>  $1,779.00  </div></body>'''
        self.assertIsNone(page._build_text_diff(before, after))
        self.assertTrue(page._build_diff(before, after))

    def test_detects_price_inventory_configuration_and_availability(self):
        before = '<body><p>MacBook Air 16GB</p><p>$1,779.00</p><p>In stock</p></body>'
        for after in (
            before.replace('$1,779.00', '$1,699.00'),
            before.replace('16GB', '24GB'),
            before.replace('In stock', 'Sold out'),
            before.replace('</body>', '<p>MacBook Air 32GB</p></body>'),
            '<body></body>',
        ):
            with self.subTest(after=after):
                diff = page._build_text_diff(before, after)
                self.assertTrue(diff)
                self.assertNotIn('<p>', diff)

    def test_monitor_uses_existing_cache_and_preserves_baseline_across_outage(self):
        client = FakeOpenAIClient('Price changed.')
        good = lambda html: page.PageState('http', 200, None, None, html)
        with TemporaryDirectory() as directory:
            jobs_path = Path(directory) / 'jobs.json'
            cache_path = Path(directory) / 'cache'
            cache_path.write_text('<body><p>$100</p><script>old</script></body>')
            botlib.add_job('https://example.com', 'body', jobs_path, mode='text')
            self.assertEqual(botlib.load_jobs(jobs_path)[0].mode, 'text')
            states = [
                good('<body><p>$100</p><script>new</script></body>'),
                page.PageState('http', 503, None, None, None),
                page.PageState('transport_error', None, 'timeout', 'timeout', None),
                good('<body><p>$90</p></body>'),
            ]
            with patch.object(page, 'get_cache_path', return_value=cache_path), \
                    patch.object(page, 'fetch_page_state', side_effect=states), \
                    patch.object(botlib, 'send_telegram_message') as send:
                counts = [botlib.monitor_jobs_once(
                    openai_client=client, bot_token='test', chat_id='test',
                    jobs_path=jobs_path,
                ) for _ in states]
            self.assertEqual(counts, [0, 0, 0, 1])
            send.assert_called_once()
            self.assertEqual(len(client.responses.calls), 1)
            self.assertNotIn('State changed', send.call_args.kwargs['body'])
            self.assertIn('-$100', client.responses.calls[0]['input'])
            self.assertIn('+$90', client.responses.calls[0]['input'])

    def test_first_fetch_is_silent_and_standard_still_reports_status(self):
        with TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache'
            with patch.object(page, 'get_cache_path', return_value=cache), \
                    patch.object(page, 'fetch_page_state') as fetch:
                fetch.return_value = page.PageState('http', 200, None, None, '<body>Hi</body>')
                self.assertFalse(page.check_page_for_changes('url', 'body', mode='text').changed)
                fetch.return_value = page.PageState('http', 404, None, None, None)
                self.assertTrue(page.check_page_for_changes('url', 'body').status_changed)
