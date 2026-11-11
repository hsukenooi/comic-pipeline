"""BUI-1282: `ebay-sold-comps --classify-titles`, the keyless mode comic-fmv
uses to classify PriceCharting slab titles with this module's guards."""

import json

import sold_comps


def test_classify_slab_titles_mirrors_the_slab_guards():
    out = sold_comps.classify_slab_titles([
        {"title": "X-Men #96 CGC 9.4 White Pages 1975", "issue": "96"},
        {"title": "X-Men #96 VF 8.0 1975", "issue": "96"},
        {"title": "X-Men #96 CGC SS 9.0 signed Claremont", "issue": "96"},
    ])
    assert out[0] == {"grade": 9.4, "certifier": "cgc", "label": "universal",
                      "page_quality": "white", "exclude_code": None}
    assert out[1]["certifier"] is None and out[1]["exclude_code"] == "not_graded"
    assert out[2]["label"] == "signature_series"


def test_classify_titles_cli_needs_no_api_key(monkeypatch, tmp_path, capsys):
    def no_key():
        raise AssertionError("classify mode must not load the API key")

    monkeypatch.setattr(sold_comps, "load_sold_comps_key", no_key)
    src = tmp_path / "in.json"
    src.write_text(json.dumps([{"title": "X-Men #96 CGC 9.4", "issue": "96"}]))
    assert sold_comps.main(["--classify-titles", str(src)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["grade"] == 9.4
