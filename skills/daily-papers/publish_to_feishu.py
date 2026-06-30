#!/usr/bin/env python3
"""
Publish daily paper Markdown files to Feishu docs with larksuite/cli.

The script creates Feishu docs from existing Obsidian Markdown files. It keeps a
small registry in the DailyPapers folder so reruns do not create duplicate docs.
"""

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

_SHARED_DIR = Path(__file__).resolve().parent.parent / "_shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

from user_config import daily_papers_dir, feishu_config, paper_notes_dir


def load_registry(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def save_registry(path: Path, registry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
        f.write("\n")


def strip_frontmatter(markdown: str) -> str:
    return re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, flags=re.DOTALL)


def convert_obsidian_links(markdown: str) -> str:
    """Convert Obsidian wikilinks to plain Markdown-ish text for Feishu import."""

    def replace_link(match: re.Match) -> str:
        body = match.group(1)
        if "|" in body:
            _target, alias = body.split("|", 1)
            return alias.strip()
        return body.strip()

    return re.sub(r"\[\[([^\]]+)\]\]", replace_link, markdown)


def extract_markdown_images(markdown: str) -> List[Tuple[str, str]]:
    current_title = ""
    images = []
    for line in markdown.splitlines():
        heading = re.match(r"^###\s+\d+\.\s+(.+)$", line)
        if heading:
            current_title = heading.group(1).strip()
            continue
        image = re.match(r"^!\[([^\]]*)\]\((https?://[^)]+)\)\s*$", line)
        if image:
            alt, url = image.groups()
            images.append((current_title or alt or "image", url))
    return images


def remove_markdown_images(markdown: str) -> str:
    return re.sub(r"^!\[[^\]]*\]\(https?://[^)]+\)\s*$\n?", "", markdown, flags=re.MULTILINE)


def compact_recommendation_markdown(markdown: str) -> str:
    """Keep recommendation signal high while avoiding Feishu import timeouts."""
    output = []
    drop_prefixes = ("- **作者**:", "- **机构**:", "- **核心方法**:")
    for line in markdown.splitlines():
        if line.startswith(drop_prefixes):
            continue
        output.append(line)
    return "\n".join(output) + "\n"


def prepare_markdown(source: Path, temp_dir: Path, *, include_images: bool = True, compact: bool = False) -> Path:
    temp_dir.mkdir(parents=True, exist_ok=True)
    content = source.read_text(encoding="utf-8")
    content = strip_frontmatter(content)
    content = convert_obsidian_links(content)
    if compact:
        content = compact_recommendation_markdown(content)
    if not include_images:
        content = remove_markdown_images(content)

    digest = hashlib.sha1(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
    suffix = "withimg" if include_images else "noimg"
    output = temp_dir / f"{source.stem}-{suffix}-{digest}.md"
    output.write_text(content, encoding="utf-8")
    return output


def extract_required_note_names(recommendation_path: Path) -> List[str]:
    content = recommendation_path.read_text(encoding="utf-8")
    required = []

    table_match = re.search(r"^## 分流表$.+?(?=^## |\Z)", content, re.MULTILINE | re.DOTALL)
    if table_match:
        for line in table_match.group(0).splitlines():
            if "🔥 必读" not in line:
                continue
            required.extend(re.findall(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", line))

    for note_name in re.findall(r"- 📒 \*\*笔记\*\*: \[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content):
        if note_name not in required:
            required.append(note_name)

    return required


def find_note_file(note_name: str) -> Optional[Path]:
    notes_dir = paper_notes_dir()
    if not notes_dir.exists():
        return None

    normalized = note_name.lower()
    for md_file in notes_dir.rglob("*.md"):
        if "_概念" in str(md_file):
            continue
        if md_file.stem.lower() == normalized:
            return md_file
    return None


def build_create_command(cli: str, cfg: dict, title: str, markdown_filename: str) -> List[str]:
    resolved_cli = shutil.which(cli) or cli
    cmd = [
        resolved_cli,
        "docs",
        "+create",
        "--doc-format",
        "markdown",
        "--title",
        title,
        "--content",
        f"@{markdown_filename}",
    ]

    for key, value in (
        ("--as", cfg.get("as")),
        ("--parent-token", cfg.get("parent_token")),
        ("--parent-position", cfg.get("parent_position")),
    ):
        if value:
            cmd.extend([key, str(value)])

    return cmd


def parse_created_document(stdout: str) -> dict:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return {}
    document = payload.get("data", {}).get("document", {})
    return {
        "document_id": document.get("document_id", ""),
        "url": document.get("url", ""),
        "raw": payload,
    }


def create_doc_from_markdown(source: Path, title: str, cfg: dict, temp_dir: Path, *, include_images: bool) -> dict:
    prepared = prepare_markdown(
        source,
        temp_dir,
        include_images=include_images,
        compact=source.name.endswith("论文推荐.md"),
    )
    cmd = build_create_command(cfg.get("cli", "lark-cli"), cfg, title, prepared.name)
    completed = subprocess.run(cmd, cwd=temp_dir, text=True, capture_output=True, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    parsed = parse_created_document(completed.stdout)
    parsed["stdout"] = completed.stdout.strip()
    return parsed


def append_images_to_doc(document_id: str, source: Path, cfg: dict, temp_dir: Path) -> dict:
    markdown = strip_frontmatter(source.read_text(encoding="utf-8"))
    images = extract_markdown_images(markdown)
    if not images:
        return {"attempted": 0, "inserted": 0, "failed": []}

    cli = shutil.which(cfg.get("cli", "lark-cli")) or cfg.get("cli", "lark-cli")
    failures = []
    inserted = 0

    header = "<h2>Paper Image Appendix</h2><p>Images are from arXiv HTML pages, ordered by the recommendation list.</p>"
    header_cmd = [
        cli,
        "docs",
        "+update",
        "--doc",
        document_id,
        "--command",
        "append",
        "--content",
        header,
    ]
    if cfg.get("as"):
        header_cmd.extend(["--as", str(cfg.get("as"))])
    subprocess.run(header_cmd, text=True, capture_output=True, encoding="utf-8")

    local_dir = temp_dir / "images"
    local_dir.mkdir(parents=True, exist_ok=True)

    for idx, (caption, url) in enumerate(images, 1):
        label = f"{idx}. {caption}"
        xml = (
            f"<p><b>{escape_xml(label)}</b></p>"
            f"<img href=\"{escape_xml(url)}\" caption=\"{escape_xml(label)}\"/>"
        )
        cmd = [
            cli,
            "docs",
            "+update",
            "--doc",
            document_id,
            "--command",
            "append",
            "--content",
            xml,
        ]
        if cfg.get("as"):
            cmd.extend(["--as", str(cfg.get("as"))])
        completed = subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8")
        if completed.returncode == 0:
            inserted += 1
            time.sleep(1)
            continue

        try:
            local_file = download_image(url, local_dir, idx)
            media_cmd = [
                cli,
                "docs",
                "+media-insert",
                "--doc",
                document_id,
                "--file",
                local_file.name,
                "--caption",
                label,
                "--align",
                "center",
                "--width",
                "800",
            ]
            if cfg.get("as"):
                media_cmd.extend(["--as", str(cfg.get("as"))])
            media = subprocess.run(
                media_cmd,
                cwd=local_dir,
                text=True,
                capture_output=True,
                encoding="utf-8",
            )
            if media.returncode == 0:
                inserted += 1
            else:
                failures.append({"caption": label, "url": url, "error": media.stderr.strip() or media.stdout.strip()})
        except Exception as exc:
            failures.append({"caption": label, "url": url, "error": str(exc)})
        time.sleep(1)

    return {"attempted": len(images), "inserted": inserted, "failed": failures}


def escape_xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def download_image(url: str, output_dir: Path, idx: int) -> Path:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
        suffix = ".png"
    output = output_dir / f"{idx:02d}{suffix}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        output.write_bytes(resp.read())
    return output


def publish_file(source: Path, title: str, cfg: dict, registry: dict, temp_dir: Path, dry_run: bool) -> dict:
    source_key = str(source.resolve())
    if source_key in registry:
        return {"status": "skipped", "title": title, "source": str(source), "result": registry[source_key]}

    prepared = prepare_markdown(
        source,
        temp_dir,
        include_images=True,
        compact=source.name.endswith("论文推荐.md"),
    )
    cmd = build_create_command(cfg.get("cli", "lark-cli"), cfg, title, prepared.name)

    if dry_run:
        return {"status": "dry-run", "title": title, "source": str(source), "command": cmd}

    try:
        created = create_doc_from_markdown(source, title, cfg, temp_dir, include_images=True)
        image_result = {"attempted": 0, "inserted": 0, "failed": []}
    except RuntimeError as exc:
        if "timeout" not in str(exc).lower() and "server time out" not in str(exc).lower():
            raise RuntimeError(f"Feishu publish failed for {source}: {exc}") from exc
        created = create_doc_from_markdown(source, title, cfg, temp_dir, include_images=False)
        if not created.get("document_id"):
            raise RuntimeError(f"Feishu publish created no document id for {source}")
        image_result = append_images_to_doc(created.get("document_id", ""), source, cfg, temp_dir)

    result = {
        "title": title,
        "source": str(source),
        "published_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stdout": created.get("stdout", ""),
        "url": created.get("url", ""),
        "document_id": created.get("document_id", ""),
        "images": image_result,
    }
    registry[source_key] = result
    return {"status": "created", **result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish daily paper Markdown files to Feishu docs")
    parser.add_argument("--recommendation", required=True, help="Path to YYYY-MM-DD-论文推荐.md")
    parser.add_argument("--date", help="Date used in generated titles, defaults to today")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without creating docs")
    args = parser.parse_args()

    cfg = feishu_config()
    if not cfg.get("enabled") and not args.dry_run:
        print("Feishu publishing is disabled in config")
        return

    recommendation_path = Path(args.recommendation).expanduser().resolve()
    if not recommendation_path.exists():
        raise FileNotFoundError(f"Recommendation file not found: {recommendation_path}")

    run_date = args.date or dt.date.today().isoformat()
    registry_path = daily_papers_dir() / cfg.get("registry_file", ".feishu-published.json")
    registry = load_registry(registry_path)
    temp_dir = daily_papers_dir() / ".feishu_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    title_prefix = cfg.get("title_prefix", "每日论文推荐")
    planned_files = []
    if cfg.get("publish_recommendation", True):
        planned_files.append((recommendation_path, f"{title_prefix} {run_date}"))

    if cfg.get("publish_required_notes", True):
        for note_name in extract_required_note_names(recommendation_path):
            note_file = find_note_file(note_name)
            if note_file:
                planned_files.append((note_file, f"{run_date} 精读笔记 - {note_file.stem}"))
            else:
                print(f"Warning: note not found for Feishu publishing: {note_name}", file=sys.stderr)

    results = []
    for source, title in planned_files:
        results.append(publish_file(source, title, cfg, registry, temp_dir, args.dry_run))

    if not args.dry_run:
        save_registry(registry_path, registry)

    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
