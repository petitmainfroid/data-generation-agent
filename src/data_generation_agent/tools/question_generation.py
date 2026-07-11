from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from data_generation_agent.generation import GenerationError, GenerationPolicy, GenerationRequest, GenerationRunner
from data_generation_agent.harness.artifacts import ArtifactStore
from data_generation_agent.harness.db import initialize_database
from data_generation_agent.knowledge.store import KnowledgeStore
from data_generation_agent.providers import ModelGatewayError, OpenAICompatibleGateway


def parse_args(argv: list[str] | None=None) -> argparse.Namespace:
    parser=argparse.ArgumentParser(description="Grounded one-question generation.")
    parser.add_argument("--input",type=Path,required=True); parser.add_argument("--db",type=Path,required=True); parser.add_argument("--artifact-root",type=Path,required=True)
    parser.add_argument("--model",default="gpt-5.5"); parser.add_argument("--max-tokens",type=int,default=8192); parser.add_argument("--timeout",type=int,default=600); parser.add_argument("--allow-insecure-http",action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    raw=json.loads(args.input.read_text(encoding="utf-8"))
    request=GenerationRequest(job_id=raw["job_id"],seed_id=raw["seed_id"],seed_question=raw["seed_question"],question_type=raw["question_type"],persona_snapshot_id=raw["persona_snapshot_id"],approved_snapshot_ids=tuple(raw["approved_snapshot_ids"]),retrieval_query=raw["retrieval_query"])
    gateway=OpenAICompatibleGateway(base_url=os.environ.get("DEEPSEEK_BASE_URL",""),api_key=os.environ.get("DEEPSEEK_API_KEY",""),allow_insecure_http=args.allow_insecure_http)
    connection=initialize_database(args.db)
    try:
        artifacts=ArtifactStore(args.artifact_root,connection); runner=GenerationRunner(connection,artifacts,KnowledgeStore(connection,artifacts),gateway)
        secrets=tuple(value for name,value in os.environ.items() if any(marker in name.upper() for marker in ("KEY","TOKEN","SECRET")) and len(value)>=8)
        result=runner.generate(request,GenerationPolicy(model=args.model,max_tokens=args.max_tokens,timeout_seconds=args.timeout),forbidden_values=secrets)
    finally: connection.close()
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0


def main() -> None:
    try: raise SystemExit(run(parse_args()))
    except (GenerationError,ModelGatewayError,OSError,KeyError,ValueError,json.JSONDecodeError) as exc:
        print(f"generation tool error: {exc}",file=sys.stderr); raise SystemExit(2) from exc


if __name__=="__main__": main()
