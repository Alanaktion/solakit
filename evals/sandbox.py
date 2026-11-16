"""Disposable evaluation tools. No production tools, memory, or destinations."""

import json
from pathlib import Path
from typing import Any

from evals.cases import Case


class Sandbox:
    def __init__(self, root: Path, case: Case):
        self.root = root.resolve()
        self.case = case
        self.effects: list[dict[str, str]] = []
        for name, content in case.resources.items():
            self.write(name, content)

    def path(self, name: str) -> Path:
        path = (self.root / name).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError("path outside disposable workspace")
        return path

    def write(self, name: str, content: str) -> None:
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def artifacts(self) -> dict[str, str]:
        return {
            str(path.relative_to(self.root)): path.read_text(encoding="utf-8")
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def execute(self, call: dict[str, Any]) -> dict[str, Any]:
        function = call.get("function", {})
        name = function.get("name", "")
        receipt: dict[str, Any] = {
            "call_id": call.get("id"),
            "name": name,
            "arguments": function.get("arguments"),
            "status": "invalid_arguments",
        }
        available = {tool["function"]["name"] for tool in self.case.tools}
        if name not in available:
            return receipt | {"status": "missing_tool"}
        if name in self.case.denied_tools:
            return receipt | {"status": "denied"}
        try:
            args = json.loads(function["arguments"])
            fields = {
                "runtime_info": set(),
                "read_file": {"path"},
                "write_file": {"path", "content"},
                "publish_note": {"text"},
                "recall": {"query"},
            }
            if name not in fields:
                return receipt | {"status": "missing_tool"}
            if not isinstance(args, dict) or set(args) != fields[name]:
                raise ValueError("arguments do not match tool fields")
            if any(not isinstance(value, str) for value in args.values()):
                raise ValueError("tool arguments must be strings")
            result: Any = None
            match name:
                case "runtime_info":
                    result = {"runtime": "evaluation", "disposable": True}
                case "read_file":
                    result = self.path(args["path"]).read_text(encoding="utf-8")
                case "write_file":
                    self.write(args["path"], args["content"])
                    result = {"path": args["path"]}
                case "publish_note":
                    self.effects.append({"text": args["text"]})
                    result = {"fake_destination": "notes", "sequence": len(self.effects)}
                case "recall":
                    if self.case.memory_outage:
                        return receipt | {"status": "unavailable"}
                    result = []
            return receipt | {"status": "ok", "result": result}
        except (KeyError, TypeError, ValueError, OSError) as error:
            return receipt | {"error": str(error)}
