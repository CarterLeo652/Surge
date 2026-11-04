"""Offline fixtures for visible TeleBox behavior and AstrBot Telegram delivery."""

import asyncio
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from xml.etree import ElementTree

from astrbot_plugin_vps_tools import bgp_original, dig_original, ip_original, whois_original
from astrbot_plugin_vps_tools.auto_delete import AutoDelete
from astrbot_plugin_vps_tools.group_scope import group_allowed
from astrbot_plugin_vps_tools.telegram_photo import send_bgp_photo
from astrbot_plugin_vps_tools.telegram_text import finish_progress, ip_html, send_html, whois_html, whois_history_html
from astrbot_plugin_vps_tools.whois_history import WhoisHistory


class ParityTests(unittest.TestCase):
    def test_group_scope_matches_happy_repeat_allow_and_deny_lists(self):
        self.assertFalse(group_allowed(None, [], [], enabled=False))
        self.assertFalse(group_allowed("-123", [], [], enabled=False))
        self.assertTrue(group_allowed("-123", [], []))
        self.assertTrue(group_allowed(None, ["-123"], []))
        self.assertTrue(group_allowed("-123#456", ["-123"], []))
        self.assertTrue(group_allowed("-123#456", ["-123#456"], []))
        self.assertFalse(group_allowed("-123#789", ["-123#456"], []))
        self.assertFalse(group_allowed("-789", ["-123"], []))
        self.assertFalse(group_allowed("-123#456", ["-123"], ["-123#456"]))
        self.assertFalse(group_allowed("-123#456", ["-123#456"], ["-123"]))
        self.assertTrue(group_allowed("-123#789", ["-123"], ["-123#456"]))

    def test_ip_api_request_and_original_result_layout(self):
        seen = {}
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, *_):
                return json.dumps({"status": "success", "query": "1.1.1.1",
                                   "country": "Australia", "regionName": "Queensland",
                                   "city": "Brisbane", "isp": "Cloudflare", "org": "Cloudflare",
                                   "as": "AS13335 Cloudflare", "hosting": True}).encode()
        def fake_open(request, timeout):
            seen["url"], seen["ua"], seen["timeout"] = request.full_url, request.get_header("User-agent"), timeout
            return Response()
        with patch.object(ip_original, "urlopen", side_effect=fake_open):
            data = asyncio.run(ip_original.query_ip("one.one.one.one"))
        self.assertIn("/json/one.one.one.one?lang=zh-CN&fields=", seen["url"])
        self.assertEqual(seen["ua"], "TeleBox-IP-Plugin/1.0")
        self.assertEqual(seen["timeout"], 15)
        self.assertEqual(ip_html(data, "one.one.one.one"),
                         "此 IP 可能为数据中心 IP\n\n🌍 <b>IP/域名查询结果</b>\n\n"
                         "<b>🔍 查询目标:</b> <code>1.1.1.1</code>\n"
                         "<b>📍 地理位置:</b> Australia - Queensland - Brisbane\n"
                         "<b>🏢 ISP:</b> Cloudflare\n<b>🏦 组织:</b> Cloudflare\n"
                         "<b>🔢 AS号:</b> <code>AS13335 Cloudflare</code>\n\nhttps://bgp.he.net/AS13335")

    def test_ip_does_not_invent_another_provider(self):
        self.assertIn("失败原因", ip_html({"status": "fail", "message": "invalid query"}, "bad"))
        with patch.object(ip_original, "urlopen", side_effect=OSError("network down")):
            data = asyncio.run(ip_original.query_ip("example.com"))
        self.assertEqual(data, {"status": "fail", "message": "网络请求失败"})

    def test_bgp_fallback_and_one_captioned_photo(self):
        calls = []
        def fetch(url, _limit):
            calls.append(url)
            if "1.1.1.0_24" in url:
                return b"<svg>Not_Visible in_DFZ</svg>"
            return b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
        with patch.object(bgp_original, "_fetch_bytes", side_effect=fetch), \
             patch.object(bgp_original, "_svg_to_png", return_value=b"\x89PNG"):
            image = asyncio.run(bgp_original.fetch_bgp_image("1.1.1.1"))
        self.assertEqual(image.prefix, "1.1.0.0/23")
        self.assertEqual(len(calls), 2)
        event = Mock()
        event.get_platform_name.return_value = "telegram"
        event.get_group_id.return_value = "-123#456"
        event.client.send_photo = AsyncMock()
        self.assertTrue(asyncio.run(send_bgp_photo(event, image.png, "1.1.1.1", image.prefix)))
        options = event.client.send_photo.await_args.kwargs
        self.assertEqual(options["message_thread_id"], 456)
        self.assertEqual(options["photo"].getvalue(), b"\x89PNG")
        self.assertIn("<b>BGP路由图</b>", options["caption"])
        self.assertIn("<i>使用前缀: 1.1.0.0/23</i>", options["caption"])
        event.clear_result.assert_called_once()
        event.stop_event.assert_called_once()

    def test_direct_telegram_text_clears_pipeline_reply(self):
        event = Mock()
        event.get_platform_name.return_value = "telegram"
        event.get_group_id.return_value = "-123"
        event.client.send_message = AsyncMock()
        self.assertTrue(asyncio.run(send_html(event, "<b>查询结果</b>")))
        event.client.send_message.assert_awaited_once()
        event.clear_result.assert_called_once()
        event.stop_event.assert_called_once()

    def test_auto_delete_default_off_and_enabled_for_final_result(self):
        async def check():
            event = Mock()
            event.get_platform_name.return_value = "telegram"
            event.get_group_id.return_value = "-123"
            event.client.send_message = AsyncMock(return_value=SimpleNamespace(
                chat=SimpleNamespace(id=-123), message_id=42))
            event.client.delete_message = AsyncMock()
            disabled = AutoDelete({"auto_delete": False, "auto_delete_seconds": 1})
            await send_html(event, "<b>结果</b>", on_sent=disabled.schedule)
            self.assertFalse(disabled.tasks)
            enabled = AutoDelete({"auto_delete": True, "auto_delete_seconds": 1})
            with patch("astrbot_plugin_vps_tools.auto_delete.asyncio.sleep", new=AsyncMock()):
                await send_html(event, "<b>结果</b>", on_sent=enabled.schedule)
                await asyncio.gather(*enabled.tasks)
            event.client.delete_message.assert_awaited_once_with(chat_id=-123, message_id=42)
        asyncio.run(check())

    def test_auto_delete_starts_after_progress_becomes_result(self):
        async def check():
            event = Mock()
            progress = SimpleNamespace(chat=SimpleNamespace(id=-123), message_id=43,
                                       edit_text=AsyncMock())
            event.client.delete_message = AsyncMock()
            manager = AutoDelete({"auto_delete": True, "auto_delete_seconds": 1})
            self.assertFalse(manager.tasks)
            with patch("astrbot_plugin_vps_tools.auto_delete.asyncio.sleep", new=AsyncMock()):
                self.assertTrue(await finish_progress(event, progress, "<b>结果</b>",
                                                      on_sent=manager.schedule))
                await asyncio.gather(*manager.tasks)
            progress.edit_text.assert_awaited_once()
            event.client.delete_message.assert_awaited_once_with(chat_id=-123, message_id=43)
        asyncio.run(check())

    def test_auto_delete_covers_bgp_photo(self):
        async def check():
            event = Mock()
            event.get_platform_name.return_value = "telegram"
            event.get_group_id.return_value = "-123"
            event.client.send_photo = AsyncMock(return_value=SimpleNamespace(
                chat=SimpleNamespace(id=-123), message_id=44))
            event.client.delete_message = AsyncMock()
            manager = AutoDelete({"auto_delete": True, "auto_delete_seconds": 1})
            with patch("astrbot_plugin_vps_tools.auto_delete.asyncio.sleep", new=AsyncMock()):
                self.assertTrue(await send_bgp_photo(
                    event, b"\x89PNG", "1.1.1.1", "1.1.1.0/24",
                    on_sent=manager.schedule))
                await asyncio.gather(*manager.tasks)
            event.client.delete_message.assert_awaited_once_with(chat_id=-123, message_id=44)
        asyncio.run(check())

    def test_bgp_square_image_frame_matches_reference_aspect(self):
        chart = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 329, 954)
        calls = []
        def fake_svg2png(*, bytestring, **_):
            calls.append(bytestring)
            return chart if len(calls) == 1 else b"framed"
        with patch.dict(sys.modules, {"cairosvg": SimpleNamespace(svg2png=fake_svg2png)}):
            self.assertEqual(bgp_original._svg_to_png(b"<svg/>"), b"framed")
        root = ElementTree.fromstring(calls[1])
        image = root.find("{http://www.w3.org/2000/svg}image")
        white_panel = root.findall("{http://www.w3.org/2000/svg}rect")[1]
        self.assertEqual(root.attrib["width"], root.attrib["height"])
        self.assertEqual(image.attrib["height"], "1800")
        self.assertGreater(int(image.attrib["x"]), 500)
        self.assertEqual(white_panel.attrib["fill"], "#ffffff")

    def test_bgp_dns_filter_keeps_source_order_and_duplicates(self):
        html = "<div>1.1.1.1 a.example.com 1.1.1.2 b.example.com " \
               "1.1.1.3 c.example.com 1.1.1.4 one.other.net " \
               "1.1.1.4 one.other.net</div>"
        self.assertEqual(bgp_original._extract_dns(html),
                         ["1.1.1.4\tone.other.net", "1.1.1.4\tone.other.net"])

    def test_whois_namebeta_only_and_expandable_raw(self):
        raw = "Registrar: Example Registrar\nCreation Date: 2001-01-01T00:00:00Z\nName Server: ns1.example.com"
        with patch.object(whois_original, "_fetch_namebeta", return_value=raw):
            record = asyncio.run(whois_original.query_whois("example.com"))
        self.assertEqual(record["registrar"], "Example Registrar")
        html = whois_html(record)
        self.assertIn("<blockquote expandable>", html)
        self.assertIn("<b>📄 原始 WHOIS 数据：</b>", html)
        with patch.object(whois_original, "_fetch_namebeta", side_effect=whois_original.QueryError("not found")):
            with self.assertRaises(whois_original.QueryError):
                asyncio.run(whois_original.query_whois("example.com"))

    def test_whois_batch_keeps_original_raw_and_history_markup(self):
        raw = "Registrar: Example\nFor more information see example.org"
        with patch.object(whois_original, "_fetch_namebeta", return_value=raw):
            record = asyncio.run(whois_original.query_whois_raw("example.com"))
        self.assertEqual(record["rawData"], raw)
        history = whois_history_html([{"domain": "example.com", "queryTime": record["queryTime"],
                                       "expiryDate": "2000-01-01"}], 1, 1)
        self.assertIn("   ⚠️ <b>已过期</b>", history)

    def test_whois_history_cache_and_clear(self):
        with tempfile.TemporaryDirectory() as folder:
            async def check():
                store = WhoisHistory(Path(folder))
                record = {"domain": "example.com", "queryTime": "2026-11-04T00:00:00+00:00"}
                await store.save(record)
                again = WhoisHistory(Path(folder))
                await again.load()
                self.assertEqual((await again.history())[1], 1)
                self.assertEqual(await again.clear(), (1, 1))
            asyncio.run(check())

    def test_whois_uses_source_domain_cleanup_and_discards_old_rdap_cache(self):
        self.assertEqual(whois_original.normalize_domain("https://www.Example.com/path"), "Example.com")
        self.assertEqual(whois_original.clean_domain("https://www.Example.com/path"), "Example.com")
        with self.assertRaises(whois_original.QueryError):
            whois_original.normalize_domain("例子.测试")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "whois_data.json"
            path.write_text(json.dumps({"history": [], "cache": {
                "old.example": {"domain": "old.example", "source": "RDAP"},
                "new.example": {"domain": "new.example", "source": "WHOIS"},
            }}), encoding="utf-8")
            async def check():
                store = WhoisHistory(Path(folder))
                await store.load()
                self.assertNotIn("old.example", store.data["cache"])
                self.assertIn("new.example", store.data["cache"])
            asyncio.run(check())

    def test_dig_allows_original_flags_and_rejects_original_bad_chars(self):
        args, short = dig_original._validate_args("example.com @127.0.0.1 MX +noall +answer")
        self.assertEqual(args, ["example.com", "@127.0.0.1", "MX", "+noall", "+answer"])
        self.assertFalse(short)
        self.assertEqual(dig_original._validate_args("example.com A"), (["example.com", "A", "+short"], True))
        with self.assertRaises(dig_original.QueryError):
            dig_original._validate_args("example.com +short;whoami")

    def test_dig_short_and_detailed_telegram_layout(self):
        with patch.object(dig_original, "_run_dig", new=AsyncMock(return_value="1.1.1.1\n2.2.2.2")):
            with patch.object(dig_original, "_location", new=AsyncMock(return_value="Australia-AS13335")):
                result = asyncio.run(dig_original.lookup_dig_native("example.com A"))
        self.assertIn("🔍 <b>DNS 解析</b> - <code>example.com</code>\n", result)
        self.assertIn("📍 <code>1.1.1.1</code>\n   <i>Australia-AS13335</i>", result)
        output = ";; ANSWER SECTION:\nexample.com. 300 IN MX 10 mail.example.com.\n\n"
        with patch.object(dig_original, "_run_dig", new=AsyncMock(return_value=output)):
            result = asyncio.run(dig_original.lookup_dig_native("example.com MX +noall +answer"))
        self.assertIn("📧 <b>example.com</b> → <b>MX</b>: <code>10 mail.example.com.</code>", result)
        self.assertIn("<i>TTL: 300s</i>", result)


if __name__ == "__main__":
    unittest.main()
