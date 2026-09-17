from types import SimpleNamespace


class _FixtureLLM:
    def invoke(self, prompt):
        assert "OKX snapshot" in prompt
        return SimpleNamespace(content=(
            '{"decision":"Hold","confidence":0.42,'
            '"summary":"fixture summary", "trend":"range",'
            '"evidence":["OKX candles"], "risks":["fixture risk"],'
            '"invalidations":["snapshot stale"], "time_horizon":"intraday",'
            '"limitations":[]}'
        ))


class _FixtureClient:
    def get_llm(self):
        return _FixtureLLM()


def create_llm_client(provider, model, base_url=None, **kwargs):
    return _FixtureClient()
