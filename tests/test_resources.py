"""Offline tests for bounded web research and stock-footage discovery."""

import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from shortform_studio.planning import _normalize_claims, _source_snapshot
from shortform_studio.resources import _safe_public_url, collect_web_research, search_pexels_videos


class ResourceTests(unittest.TestCase):
    """Verify resource adapters without calling external providers."""

    def test_public_url_validation_rejects_local_and_credential_urls(self) -> None:
        """@brief Keep local addresses and embedded credentials out of source records."""
        self.assertIsNone(_safe_public_url("http://example.com/article"))
        self.assertIsNone(_safe_public_url("https://127.0.0.1/private"))
        self.assertIsNone(_safe_public_url("https://user:pass@example.com/article"))
        self.assertIsNone(_safe_public_url("https://gateway.local/admin"))
        self.assertEqual(_safe_public_url("https://Example.com/article#section"), "https://Example.com/article")

    def test_research_only_promotes_extracted_pages_to_evidence(self) -> None:
        """@brief Keep search snippets visible while limiting evidence IDs to fetched text."""
        search_result = {
            "results": [
                {"title": "Source A", "url": "https://example.com/a", "content": "A summary", "score": 0.9},
                {"title": "Source B", "url": "https://example.com/b", "content": "B summary", "score": 0.8},
            ]
        }
        extracted_result = {"results": [{"url": "https://example.com/a", "raw_content": "Full article evidence."}]}
        with patch("shortform_studio.resources._request_json", side_effect=[search_result, extracted_result]) as request:
            research = collect_web_research({"topic": "Coffee", "language": "English", "source_notes": ""}, "test-key")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(research["status"], "partial")
        self.assertEqual(research["evidence_source_ids"], ["S1"])
        self.assertEqual(research["sources"][0]["content"], "Full article evidence.")
        self.assertEqual(research["sources"][1]["status"], "snippet_only")

    def test_research_is_complete_only_when_every_result_is_extracted(self) -> None:
        """@brief Do not label partially fetched search results as complete research."""
        search_result = {"results": [{"title": "Source", "url": "https://example.com/a", "content": "Summary"}]}
        extracted_result = {"results": [{"url": "https://example.com/a", "raw_content": "Full page"}]}
        with patch("shortform_studio.resources._request_json", side_effect=[search_result, extracted_result]):
            research = collect_web_research({"topic": "Coffee", "language": "English", "source_notes": ""}, "test-key")
        self.assertEqual(research["status"], "complete")
        self.assertEqual(research["evidence_source_ids"], ["S1"])

    def test_research_fails_softly_when_page_extraction_fails(self) -> None:
        """@brief Preserve discovery snippets but never treat them as extracted evidence."""
        search_result = {"results": [{"title": "Source", "url": "https://example.com/a", "content": "Summary"}]}
        with patch("shortform_studio.resources._request_json", side_effect=[search_result, RuntimeError("temporary failure")]):
            research = collect_web_research({"topic": "Coffee", "language": "English", "source_notes": ""}, "test-key")
        self.assertEqual(research["status"], "partial")
        self.assertEqual(research["evidence_source_ids"], [])
        self.assertEqual(research["sources"][0]["status"], "snippet_only")
        self.assertIn("temporary failure", research["extraction_error"])

    def test_operator_urls_are_prioritized_and_limited(self) -> None:
        """@brief Extract at most two safe operator URLs before search-discovered pages."""
        search_result = {"results": [{"title": "Search", "url": "https://search.example/page", "content": "summary"}]}
        extracted_result = {"results": []}
        with patch("shortform_studio.resources._request_json", side_effect=[search_result, extracted_result]) as request:
            research = collect_web_research(
                {"topic": "Coffee", "language": "Korean", "source_notes": "https://first.example/a https://second.example/b https://third.example/c"},
                "test-key",
            )
        urls = request.call_args.kwargs["body"]["urls"]
        self.assertEqual(urls, ["https://first.example/a", "https://second.example/b", "https://search.example/page"])
        self.assertEqual(research["sources"][0]["url"], "https://first.example/a")

    def test_plan_source_snapshot_and_claims_reject_unknown_citations(self) -> None:
        """@brief Keep only fetched source snapshots and remove fabricated source IDs."""
        sources = [
            {"id": "S1", "url": "https://example.com/a", "content": "Evidence", "status": "fetched"},
            {"id": "S2", "url": "https://example.com/b", "content": "Snippet", "status": "snippet_only"},
        ]
        snapshot = _source_snapshot(sources)
        claims = _normalize_claims(
            [{"claim": "Fact", "source_ids": ["S1", "S999"], "evidence": "Text"}],
            {source["id"] for source in snapshot},
        )
        self.assertEqual([source["id"] for source in snapshot], ["S1"])
        self.assertEqual(claims[0]["source_ids"], ["S1"])
        self.assertFalse(claims[0]["evidence_required"])

    def test_pexels_search_is_per_scene_and_marks_rights_for_human_review(self) -> None:
        """@brief Preserve scene mapping and licensing provenance without downloading clips."""
        response = {
            "videos": [
                {
                    "id": 42,
                    "url": "https://www.pexels.com/video/example-42/",
                    "image": "https://images.pexels.com/videos/42/poster.jpeg",
                    "duration": 12,
                    "width": 720,
                    "height": 1280,
                    "user": {"name": "Creator", "url": "https://www.pexels.com/@creator"},
                }
            ]
        }
        with patch("shortform_studio.resources._get_json", return_value=response) as request:
            materials = search_pexels_videos([{"id": "scene-1", "visual_query": "coffee pouring"}], "test-key")
        request_url = request.call_args.args[0]
        query = parse_qs(urlsplit(request_url).query)
        self.assertEqual(query["orientation"], ["portrait"])
        self.assertEqual(query["query"], ["coffee pouring"])
        self.assertEqual(materials[0]["scene_id"], "scene-1")
        self.assertEqual(materials[0]["rights_status"], "human_verification_required")
        self.assertEqual(materials[0]["author"], "Creator")

    def test_missing_provider_credentials_are_explicit(self) -> None:
        """@brief Require explicit provider setup before making external requests."""
        with self.assertRaisesRegex(ValueError, "TAVILY_API_KEY"):
            collect_web_research({"topic": "Coffee"}, "")
        with self.assertRaisesRegex(ValueError, "PEXELS_API_KEY"):
            search_pexels_videos([{"visual_query": "coffee"}], "")


if __name__ == "__main__":
    unittest.main()
