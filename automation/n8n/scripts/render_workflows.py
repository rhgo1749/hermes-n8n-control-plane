#!/usr/bin/env python3
from __future__ import annotations
import argparse, ipaddress, json, sys, uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "workflows"
ROUTER_FALLBACK_URL = "http://127.0.0.1:5681/fallback"
FALLBACK_TIMEOUT_MS = 120_000
_NAMESPACE = uuid.UUID("4d3669cd-39ce-4c84-a10d-762278d838c6")
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
ACTIVE_JOBS = ({"slug":"github-agent-ready-intake","profile":"default","id":"bf431b2a6ba6","name":"GitHub agent-ready Issue intake","schedule":"*/5 * * * *"},)
def _uuid(key:str)->str: return str(uuid.uuid5(_NAMESPACE,key))
def _base_workflow(name,nodes,connections): return {"name":name,"nodes":nodes,"pinData":{},"connections":connections,"active":False,"settings":{"executionOrder":"v1"},"meta":{"templateCredsSetupCompleted":False},"tags":[]}
def schedule_workflow(job):
    schedule_name="Schedule Trigger"; fallback_name="Run registry fallback"
    schedule={"parameters":{"rule":{"interval":[{"field":"cronExpression","expression":job["schedule"]}]}},"id":_uuid(f"{job['id']}:schedule"),"name":schedule_name,"type":"n8n-nodes-base.scheduleTrigger","typeVersion":1.2,"position":[180,300]}
    fallback={"parameters":{"method":"POST","url":ROUTER_FALLBACK_URL,"authentication":"genericCredentialType","genericAuthType":"httpHeaderAuth","options":{"timeout":FALLBACK_TIMEOUT_MS,"response":{"response":{"neverError":False,"responseFormat":"json","fullResponse":True}}}},"id":_uuid(f"{job['id']}:registry-fallback"),"name":fallback_name,"type":"n8n-nodes-base.httpRequest","typeVersion":4.2,"position":[440,300]}
    return _base_workflow(f"Hermes schedule · {job['name']}",[schedule,fallback],{schedule_name:{"main":[[{"node":fallback_name,"type":"main","index":0}]]}})
def _write_json(path,payload): path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def write_templates(directory):
    written=[]
    for job in ACTIVE_JOBS:
        path=directory/f"schedule-{job['slug']}.json"; _write_json(path,schedule_workflow(job)); written.append(path)
    return written
def _is_private_http_target(host):
    if host=="localhost": return True
    try: address=ipaddress.ip_address(host)
    except ValueError: return False
    return address.is_loopback or address.is_private or address in _TAILSCALE_CGNAT
def normalize_dashboard_url(raw):
    value=raw.strip().rstrip("/"); parsed=urlparse(value)
    if parsed.scheme not in {"http","https"} or not parsed.netloc: raise ValueError("dashboard URL must be absolute")
    if parsed.username or parsed.password or not parsed.hostname: raise ValueError("unsafe dashboard URL")
    if parsed.scheme=="http" and not _is_private_http_target(parsed.hostname): raise ValueError("http dashboard URL must be private")
    return value
def render_templates(template_dir,output_dir,dashboard_url):
    normalize_dashboard_url(dashboard_url); templates=sorted(template_dir.glob("*.json")); rendered=[]
    if not templates: raise ValueError("no workflow templates found")
    for template in templates:
        data=json.loads(template.read_text(encoding="utf-8")); path=output_dir/template.name; _write_json(path,data); rendered.append(path)
    return rendered
def main(argv=None):
    parser=argparse.ArgumentParser(); mode=parser.add_mutually_exclusive_group(required=True); mode.add_argument("--write-templates",action="store_true"); mode.add_argument("--dashboard-url"); parser.add_argument("--template-dir",type=Path,default=WORKFLOWS); parser.add_argument("--output-dir",type=Path); args=parser.parse_args(argv)
    try:
        if args.write_templates: output=args.output_dir or WORKFLOWS; paths=write_templates(output)
        else: output=args.output_dir or ROOT/"state"/"rendered-workflows"; paths=render_templates(args.template_dir,output,args.dashboard_url)
    except Exception as exc: print(f"workflow-render: ERROR: {exc}",file=sys.stderr); return 1
    print(json.dumps({"count":len(paths),"output_dir":str(output),"files":[p.name for p in paths]},ensure_ascii=False)); return 0
if __name__=="__main__": raise SystemExit(main())
