import subprocess
import unittest
from unittest.mock import patch

from mealapp import speech, web


URLS=(
    "https://www.facebook.com/share/v/1KFvMHBqtv/",
    "https://www.facebook.com/share/r/abc/",
    "https://www.facebook.com/watch/?v=123456",
    "https://m.facebook.com/watch?v=123456",
    "https://www.facebook.com/chef/videos/123456/",
    "https://web.facebook.com/reel/123456",
    "https://www.facebook.com/video.php?v=123456",
    "https://fb.watch/abc123/",
    "https://www.facebook.com/plugins/video.php?href=https%3A%2F%2Fwww.facebook.com%2Fchef%2Fvideos%2F123",
)


class FacebookTests(unittest.TestCase):
    def test_common_facebook_video_formats_are_recognized(self):
        for url in URLS:
            with self.subTest(url=url):
                self.assertTrue(web.facebook_video(url))

    def test_unrelated_pages_and_lookalikes_are_not_video_links(self):
        for url in ("https://example.com/reel/123", "https://facebook.com.evil.test/watch?v=1", "https://notfacebook.com/videos/1", "https://www.facebook.com/chef/about", "https://fb.watch/"):
            with self.subTest(url=url):
                self.assertFalse(web.facebook_video(url))

    def test_auto_detection_uses_same_video_workflow_without_article_fetch(self):
        for url in URLS:
            with self.subTest(url=url), patch.object(web,"video_source",return_value={"text":"Vietnamese recipe transcript","method":"audio"}) as video, patch.object(web,"article_text") as article:
                result=web.extract(url)
                self.assertEqual(result["method"],"audio")
                video.assert_called_once_with(url)
                article.assert_not_called()

    def test_facebook_without_captions_falls_back_to_local_audio(self):
        with patch.object(web,"public_url"),patch.object(web.subprocess,"run",return_value=subprocess.CompletedProcess([],0)),patch.object(speech,"transcribe_video",return_value="Nấu đậu hũ với cà chua.") as audio:
            result=web.extract(URLS[0])
        audio.assert_called_once_with(URLS[0])
        self.assertEqual(result["text"],"Nấu đậu hũ với cà chua.")


if __name__=="__main__":
    unittest.main()
