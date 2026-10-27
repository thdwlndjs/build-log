"""Scale measurements for Buildlog (spec §13): 100 / 1,000 / 10,000 pages.

Builds synthetic stores through the normal ingest path, then times local operations at least 30
times each: query, read + Markdown render, neighbors, graph data, the graph HTTP API, and the review
page in Chromium. Remote research time is not part of any number here.

    python tools/benchmark.py --out docs/benchmark.json [--sizes 100 1000 10000] [--runs 30] [--no-browser]
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from http.client import HTTPConnection
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from buildlog.graph import graph  # noqa: E402
from buildlog.read import read_object, render_markdown  # noqa: E402
from buildlog.search import neighbors, query  # noqa: E402
from buildlog.server import start_in_thread  # noqa: E402
from buildlog.store import ingest  # noqa: E402

WORDS = ("설치 오류 실패 경로 권한 인증서 프록시 네트워크 서비스 리스너 컨테이너 빌드 캐시 버전 충돌 환경변수 인코딩 타임아웃 메모리 포트 "
         "데이터베이스 마이그레이션 트랜잭션 인덱스 파티션 조인 집계 스키마 로그 배포 롤백 재시작 동기화 설정 토큰만료 응답지연 "
         "pip npm docker wsl git python node sqlite oracle postgres listener certificate proxy timeout cache build deploy").split()
CODES = [f"ORA-{n:05d}" for n in range(1, 120)] + [f"ERR_{n}" for n in range(1, 120)] + [f"E{n:04d}" for n in range(1, 120)]
QUERIES = [("ORA-00017 접속 오류", None), ("인증서 프록시 설치 실패", None), ("컨테이너 포트 충돌", None), ("캐시", None),
           ("빌드 캐시 버전 충돌 롤백", None), ("ERR_42", None), ("타임아웃 응답지연 재시작", None), ("설치 오류", None),
           ("docker wsl 네트워크", None), ("파티션 집계 조인", "bench")]


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))
    return ordered[k]


def timed(fn: Callable[[], Any], runs: int) -> dict[str, float]:
    fn()  # warm-up, not counted
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {"runs": runs, "median_ms": round(statistics.median(samples), 1), "p95_ms": round(percentile(samples, 95), 1),
            "max_ms": round(max(samples), 1)}


def build_store(path: Path, pages: int, seed: int = 7) -> dict[str, Any]:
    rng = random.Random(seed)
    batch = 500
    concept_ids: list[str] = []
    created: list[str] = []
    started = time.perf_counter()
    for start in range(0, pages, batch):
        count = min(batch, pages - start)
        plan: dict[str, Any] = {"idempotency_key": f"bench-{pages}-{start}",
                                "sources": [{"ref": "c", "kind": "conversation", "locator": "synthetic benchmark", "accessible_excerpt": "synthetic"}],
                                "evidence": [{"ref": "e", "source": "c", "evidence_kind": "observed", "excerpt": "synthetic observation"}],
                                "pages": [], "relations": [], "terms": []}
        for i in range(count):
            n = start + i
            words = rng.sample(WORDS, 6)
            code = rng.choice(CODES)
            kind = "concept" if n % 25 == 0 else "experience"
            body = "## 문제\n\n" + " ".join(rng.choices(WORDS, k=140)) + f"\n\n{code}\n\n## 환경\n\nWindows 11\n\n## Mitigation\n\n" + \
                   " ".join(rng.choices(WORDS, k=60))
            plan["pages"].append({"ref": f"p{i}", "type": kind, "title": f"{words[0]} {words[1]} {words[2]} 문제 {n}",
                                  "summary_md": f"{code} {words[3]} {words[4]} 조정으로 해결", "body_md": body, "evidence": ["e"]})
            if i:
                plan["relations"].append({"source": f"p{i}", "target": f"p{rng.randrange(i)}", "type": rng.choice(["RELATED_TO", "USES", "SUPPORTS"]),
                                          "explanation_md": "synthetic link", "evidence": ["e"], "status": "inferred"})
            if concept_ids and i % 3 == 0:
                plan["relations"].append({"source": f"p{i}", "target": rng.choice(concept_ids), "type": "USES",
                                          "explanation_md": "synthetic concept link", "evidence": ["e"], "status": "inferred"})
            if i % 10 == 0:
                plan["terms"].append({"expression": f"{words[5]}{n}", "scope": "bench", "page": f"p{i}", "definition_md": "synthetic term",
                                      "evidence": ["e"], "status": "inferred"})
        result = ingest(path, plan)
        for page in result["created"]["pages"]:
            created.append(page["id"])
            if page["type"] == "concept":
                concept_ids.append(page["id"])
    return {"pages": len(created), "build_seconds": round(time.perf_counter() - started, 1), "page_ids": created,
            "run_id": result["run_id"]}


def http_graph(server: Any, path: str) -> None:
    conn = HTTPConnection("127.0.0.1", server.port, timeout=30)
    conn.request("GET", path, headers={"Host": f"127.0.0.1:{server.port}"})
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 200, response.status


def browser_ready(url: str, runs: int) -> dict[str, Any] | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    samples, nodes = [], 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(url)
        page.wait_for_selector("body[data-ready=true]")  # warm-up
        for _ in range(runs):
            start = time.perf_counter()
            page.goto(url)
            page.wait_for_selector("body[data-ready=true]")
            samples.append((time.perf_counter() - start) * 1000)
            nodes = page.evaluate("window.buildlogCy.nodes().length")
        browser.close()
    return {"runs": runs, "median_ms": round(statistics.median(samples), 1), "p95_ms": round(percentile(samples, 95), 1),
            "max_ms": round(max(samples), 1), "nodes_drawn": nodes}


def short_term_body_scan(store: Path, runs: int) -> dict[str, Any]:
    """Cost of the substring body scan that query runs for 1-2 character terms, measured alone."""
    with closing(sqlite3.connect(store)) as c:
        def scan() -> None:
            c.execute("SELECT COUNT(*) FROM search_index WHERE body LIKE ? OR body LIKE ?", ("%설치%", "%오류%")).fetchone()
        return timed(scan, runs)


def machine() -> dict[str, Any]:
    info: dict[str, Any] = {"os": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version}
    try:
        ps = ("$c=Get-CimInstance Win32_Processor | Select-Object -First 1; $m=Get-CimInstance Win32_ComputerSystem; "
              "\"$($c.Name)|$($c.NumberOfCores)|$($c.NumberOfLogicalProcessors)|$([math]::Round($m.TotalPhysicalMemory/1GB,1))\"")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30).stdout.strip()
        name, cores, threads, ram = out.split("|")
        info.update({"cpu": name.strip(), "cores": int(cores), "threads": int(threads), "ram_gb": float(ram)})
    except Exception as error:  # noqa: BLE001
        info["hardware_error"] = str(error)
    return info


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "benchmark.json")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    report: dict[str, Any] = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "machine": machine(), "runs_per_measure": args.runs,
                              "targets": {"query_p95_ms_at_1000": 1000, "graph_p95_ms_at_1000": 500}, "sizes": {}}
    rng = random.Random(11)
    for size in args.sizes:
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "bench.sqlite3"
            built = build_store(store, size)
            ids = built["page_ids"]
            q_iter = iter(QUERIES * (args.runs // len(QUERIES) + 2))
            sample_ids = iter(rng.choices(ids, k=args.runs + 2))
            result: dict[str, Any] = {"build": {k: built[k] for k in ("pages", "build_seconds")},
                                      "store_bytes": store.stat().st_size}
            result["query"] = timed(lambda: query(store, *next(q_iter)), args.runs)
            result["read_render"] = timed(lambda: render_markdown(read_object(store, next(sample_ids))), args.runs)
            sample_ids = iter(rng.choices(ids, k=args.runs + 2))
            result["neighbors"] = timed(lambda: neighbors(store, next(sample_ids)), args.runs)
            result["graph_default"] = timed(lambda: graph(store), args.runs)
            sample_ids = iter(rng.choices(ids, k=args.runs + 2))
            result["graph_focus"] = timed(lambda: graph(store, focus=next(sample_ids)), args.runs)
            result["graph_search"] = timed(lambda: graph(store, q="인증서 프록시"), args.runs)
            result["short_term_body_scan"] = short_term_body_scan(store, args.runs)
            server = start_in_thread(store)
            try:
                result["http_graph"] = timed(lambda: http_graph(server, "/api/graph"), args.runs)
                if not args.no_browser:
                    result["browser_graph_ready"] = browser_ready(server.url, args.runs)
            finally:
                server.shutdown()
                server.server_close()
            report["sizes"][str(size)] = result
            print(size, json.dumps({k: v for k, v in result.items() if isinstance(v, dict)}, ensure_ascii=False), flush=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("written", args.out)


if __name__ == "__main__":
    main()
