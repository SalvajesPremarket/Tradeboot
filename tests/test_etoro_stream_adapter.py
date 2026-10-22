import unittest

from etoro_stream_adapter import parse_instrument_ticks


class EtoroStreamAdapterTests(unittest.TestCase):
    def test_parses_known_instrument_with_explicit_market_timestamp(self):
        frame = {
            "messages": [{
                "topic": "instrument:1001",
                "content": {
                    "lastPrice": "192.25",
                    "timestamp": "2026-10-22T12:30:01Z",
                    "bid": "192.20",
                    "ask": "192.30",
                },
            }]
        }
        self.assertEqual(parse_instrument_ticks(frame, {1001: "AAPL"}), [{
            "symbol": "AAPL",
            "instrument_id": 1001,
            "price": 192.25,
            "timestamp": "2026-10-22T12:30:01Z",
            "source": "etoro_websocket",
        }])

    def test_parses_json_string_content_and_single_topic_envelope(self):
        frame = {
            "topic": "instrument:42",
            "content": '{"price": 12.5, "time": "2026-10-22T12:30:00+00:00"}',
        }
        result = parse_instrument_ticks(frame, {42: "TEST"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["price"], 12.5)

    def test_rejects_missing_timestamp_invalid_price_and_unknown_instrument(self):
        frames = [
            {"topic": "instrument:1", "content": {"lastPrice": 12}},
            {"topic": "instrument:1", "content": {"lastPrice": 0, "timestamp": "2026-10-22T12:30:00Z"}},
            {"topic": "instrument:1", "content": {"lastPrice": "NaN", "timestamp": "2026-10-22T12:30:00Z"}},
        ]
        for frame in frames:
            self.assertEqual(parse_instrument_ticks(frame, {1: "ABC"}), [])
        valid = {"topic": "instrument:2", "content": {"lastPrice": 12, "timestamp": "2026-10-22T12:30:00Z"}}
        self.assertEqual(parse_instrument_ticks(valid, {1: "ABC"}), [])

    def test_ignores_non_market_topics_and_binary_frames(self):
        self.assertEqual(parse_instrument_ticks(b"heartbeat", {1: "ABC"}), [])
        self.assertEqual(parse_instrument_ticks({"topic": "private", "content": {"lastPrice": 1, "timestamp": "2026-10-22T12:30:00Z"}}, {1: "ABC"}), [])

    def test_supports_batched_messages(self):
        frame = {"messages": [
            {"topic": "instrument:1", "content": {"lastPrice": 10, "timestamp": "2026-10-22T12:30:00Z"}},
            {"topic": "instrument:2", "content": {"lastPrice": 20, "timestamp": "2026-10-22T12:30:00Z"}},
        ]}
        result = parse_instrument_ticks(frame, {1: "AAA", 2: "BBB"})
        self.assertEqual([tick["symbol"] for tick in result], ["AAA", "BBB"])


if __name__ == "__main__":
    unittest.main()
