#!/usr/bin/env python3
"""Configuration and source indexing for the static SHACL cube."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "dashboard-config.json"
# Older checkouts keep both families in one tree. Each family still uses the
# same relative directories as entsoe/CGMES-AP and entsoe/NC-AP (CGMES/SHACL,
# NCP/SHACL), so that tree can stand in when the dedicated clone is absent.
LEGACY_CHECKOUT = (ROOT / "../application-profiles-library").resolve()

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
DCT = "http://purl.org/dc/terms/"
PROF = "http://www.w3.org/ns/dx/prof/"
PROF_ROLE = "http://www.w3.org/ns/dx/prof/role/"


def slug(value: str, fallback: str = "item") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (text or fallback)[:120]


def artifact_name(value: str) -> str:
    return Path(unquote(urlparse(value).path)).name


def github_blob(repo: dict, subpath: str, line: int | None = None) -> str:
    base = (
        f"https://github.com/{repo['github_repository']}/blob/"
        f"{repo.get('github_ref') or 'main'}/{subpath}"
    )
    return f"{base}#L{line}" if line else base


def github_tree(repo: dict) -> str:
    return (
        f"https://github.com/{repo['github_repository']}/tree/"
        f"{repo.get('github_ref') or 'main'}"
    )


def load_config(path: Path = CONFIG_PATH) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    repositories = raw.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("dashboard-config.json must contain a non-empty repositories list")
    seen_repos: set[str] = set()
    for repo in repositories:
        repo_id = str(repo.get("id") or "")
        if not repo_id or repo_id in seen_repos:
            raise ValueError(f"Repository id is missing or duplicated: {repo_id!r}")
        seen_repos.add(repo_id)
        repo["checkout_path"] = resolve_checkout(str(repo["checkout"]))
        seen_families: set[str] = set()
        for family in repo.get("families") or []:
            family_id = str(family.get("id") or "")
            if not family_id or family_id in seen_families:
                raise ValueError(
                    f"Family id is missing or duplicated in {repo_id}: {family_id!r}"
                )
            seen_families.add(family_id)
    _section(raw, "data")
    _section(raw, "validation_history")
    _field(raw["data"], "graphdb_repository", "data.graphdb_repository")
    _field(raw["validation_history"], "base_url", "validation_history.base_url")
    _field(raw["validation_history"], "repository", "validation_history.repository")
    _field(raw["validation_history"], "graph", "validation_history.graph")
    return raw


def resolve_checkout(checkout: str) -> Path:
    """Directory that contains this repository's ``CGMES/`` or ``NCP/`` tree.

    ``checkout`` is relative to this repository, for example ``../CGMES-AP``.
    A combined ``application-profiles-library`` checkout is used when that
    directory is not present: the SHACL, RDFS, and PROF paths inside it are
    the same ones published by entsoe/CGMES-AP and entsoe/NC-AP.
    """
    path = (ROOT / checkout).resolve()
    if path.is_dir():
        return path
    if LEGACY_CHECKOUT.is_dir():
        return LEGACY_CHECKOUT
    return path


def _section(raw: dict, name: str) -> dict:
    section = raw.get(name)
    if not isinstance(section, dict) or not section:
        raise ValueError(f"dashboard-config.json must contain a non-empty {name} object")
    return section


def _field(section: dict, key: str, path: str) -> str:
    value = str(section.get(key) or "").strip()
    if not value:
        raise ValueError(f"dashboard-config.json missing {path}")
    return value


def history_settings(config: dict | None = None) -> dict:
    """History GraphDB target from dashboard-config.json.

    ``GDB_BASE_URL``, ``GDB_HISTORY_REPOSITORY``, and ``GDB_HISTORY_GRAPH``
    override the matching JSON fields so the batch script and Python readers
    stay pointed at the same place. There is no hardcoded repository name.
    """
    config = config if config is not None else load_config()
    raw = _section(config, "validation_history")
    base_url = os.environ.get("GDB_BASE_URL") or _field(
        raw, "base_url", "validation_history.base_url"
    )
    repository = os.environ.get("GDB_HISTORY_REPOSITORY") or _field(
        raw, "repository", "validation_history.repository"
    )
    graph = os.environ.get("GDB_HISTORY_GRAPH") or _field(
        raw, "graph", "validation_history.graph"
    )
    if not str(base_url).endswith("/"):
        base_url = f"{base_url}/"
    return {
        "base_url": base_url,
        "repository": repository,
        "graph": graph,
        "sparql": str(raw.get("sparql") or "").strip()
        or f"{base_url.rstrip('/')}/repositories/{repository}",
        "index": str(raw.get("index") or "").strip() or "index.html",
        "dashboard_dir": str(raw.get("dashboard_dir") or "").strip() or "dashboard",
        "keep_timings": int(raw.get("keep_timings") or 2),
        "keep_results": int(raw.get("keep_results") or 1),
    }


def data_settings(config: dict | None = None) -> dict:
    """Instance-data GraphDB settings from dashboard-config.json ``data``."""
    config = config if config is not None else load_config()
    raw = _section(config, "data")
    repository = _field(raw, "graphdb_repository", "data.graphdb_repository")
    base = history_settings(config)["base_url"].rstrip("/")
    return {
        "graphdb_repository": repository,
        "graphdb_resource": str(raw.get("graphdb_resource") or "").strip()
        or f"{base}/resource",
        "graphdb_sparql": str(raw.get("graphdb_sparql") or "").strip()
        or f"{base}/repositories/{repository}",
        "instance_github_repository": str(
            raw.get("instance_github_repository") or ""
        ).strip(),
    }


def export_shell(config: dict | None = None) -> None:
    """Print ``KEY=value`` lines for ``eval`` in the batch script."""
    history = history_settings(config)
    data = data_settings(config)
    values = {
        "GDB_URL": history["base_url"],
        "VALIDATE_REPO": data["graphdb_repository"],
        "HISTORY_REPO": history["repository"],
        "HISTORY_GRAPH": history["graph"],
    }
    for key, value in values.items():
        print(f"{key}={shlex.quote(value)}")


def list_shacl(config: dict | None = None) -> None:
    """Print one configured SHACL file path per line, catalog order."""
    catalog = build_catalog(config)
    for source in sorted(catalog["sources"].values(), key=lambda item: item["id"]):
        print(source["shacl_path"])


def _prof_record(path: Path) -> dict | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    profile_desc = None
    for desc in root.findall(f".//{{{RDF}}}Description"):
        types = {
            node.get(f"{{{RDF}}}resource", "")
            for node in desc.findall(f"{{{RDF}}}type")
        }
        if f"{PROF}Profile" in types:
            profile_desc = desc
            break
    title = ""
    if profile_desc is not None:
        title = (profile_desc.findtext(f"{{{DCT}}}title") or "").strip()

    vocabularies: list[str] = []
    shapes: list[str] = []
    for desc in root.findall(f".//{{{RDF}}}Description"):
        roles = {
            node.get(f"{{{RDF}}}resource", "")
            for node in desc.findall(f"{{{PROF}}}hasRole")
        }
        artifacts = [
            artifact_name(node.get(f"{{{RDF}}}resource", ""))
            for node in desc.findall(f"{{{PROF}}}hasArtifact")
        ]
        artifacts = [name for name in artifacts if name]
        if f"{PROF_ROLE}vocabulary" in roles:
            vocabularies.extend(name for name in artifacts if name.lower().endswith((".rdf", ".xml")))
        if roles & {f"{PROF_ROLE}constraints", f"{PROF_ROLE}validation"}:
            shapes.extend(name for name in artifacts if name.lower().endswith(".ttl"))
    if not vocabularies and not shapes:
        return None
    ontology = vocabularies[0] if vocabularies else ""
    profile_id = (
        re.sub(r"-AP-Voc-RDFS.*$", "", ontology, flags=re.I)
        if ontology
        else re.sub(r"(?:-AP-RDFS2020|-PROF)$", "", path.stem, flags=re.I)
    )
    return {
        "id": slug(profile_id),
        "name": title or profile_id.replace("_", " "),
        "key": profile_id,
        "ontology_file": ontology,
        "shape_files": sorted(set(shapes)),
        "prof_file": path.name,
    }


def _locate_file(repo: dict, dirs: list[str], filename: str) -> tuple[Path | None, str]:
    for subdir in dirs:
        candidate = repo["checkout_path"] / subdir / filename
        if candidate.is_file():
            return candidate, candidate.relative_to(repo["checkout_path"]).as_posix()
    return None, ""


def _profile_base(shape_file: str) -> str:
    return re.split(r"[-_]AP-Con", Path(shape_file).stem, maxsplit=1, flags=re.I)[0]


def build_catalog(config: dict | None = None) -> dict:
    """Return repository-aware SHACL files and PROF-derived profile mappings."""
    config = config or load_config()
    catalog = {"config": config, "repositories": [], "sources": {}, "by_filename": defaultdict(list)}
    for repo in config["repositories"]:
        repo_out = {**repo, "families_index": []}
        for family in repo.get("families") or []:
            prof_records: list[dict] = []
            for prof_dir in family.get("prof_dirs") or []:
                for path in sorted((repo["checkout_path"] / prof_dir).glob("*.rdf")):
                    record = _prof_record(path)
                    if record:
                        record["prof_subpath"] = path.relative_to(repo["checkout_path"]).as_posix()
                        ontology_path, ontology_subpath = _locate_file(
                            repo, family.get("rdfs_dirs") or [], record["ontology_file"]
                        )
                        record["ontology_path"] = ontology_path
                        record["ontology_subpath"] = ontology_subpath
                        prof_records.append(record)

            artifact_profiles: dict[str, list[dict]] = defaultdict(list)
            for profile in prof_records:
                for filename in profile["shape_files"]:
                    artifact_profiles[filename].append(profile)

            discovered: dict[str, tuple[Path, str]] = {}
            for shacl_dir in family.get("shacl_dirs") or []:
                for path in sorted((repo["checkout_path"] / shacl_dir).glob("*.ttl")):
                    discovered.setdefault(
                        path.name, (path, path.relative_to(repo["checkout_path"]).as_posix())
                    )

            overrides = family.get("profile_overrides") or {}
            profiles_by_id = {p["id"]: p for p in prof_records}
            family_sources: list[dict] = []
            shared_profile = {
                "id": "shared-constraints",
                "name": "Shared constraints (no single ontology)",
                "key": "shared-constraints",
                "ontology_file": "",
                "ontology_path": None,
                "ontology_subpath": "",
                "shape_files": [],
                "prof_file": "",
                "prof_subpath": "",
            }
            for filename, (path, subpath) in discovered.items():
                override = overrides.get(filename)
                candidates = artifact_profiles.get(filename, [])
                shape_base = _profile_base(filename).lower()
                exact = [
                    p for p in prof_records
                    if p["key"].lower().split("_")[-1] == shape_base.split("_")[-1]
                    or p["key"].lower() == shape_base
                ]
                if override:
                    profile = profiles_by_id.get(slug(str(override)))
                    if profile is None:
                        raise ValueError(
                            f"Unknown profile override {override!r} for {family['id']}/{filename}"
                        )
                elif len(exact) == 1:
                    profile = exact[0]
                elif len(candidates) == 1:
                    profile = candidates[0]
                elif len(candidates) > 1:
                    profile = shared_profile
                else:
                    heuristic = [
                        p for p in prof_records
                        if p["key"].lower().split("_")[-1] in shape_base
                    ]
                    profile = heuristic[0] if len(heuristic) == 1 else shared_profile
                source_id = f"{repo['id']}::{family['id']}::{filename}"
                source = {
                    "id": source_id,
                    "repository_id": repo["id"],
                    "repository_title": repo.get("title") or repo["id"],
                    "family_id": family["id"],
                    "family_title": family.get("title") or family["id"],
                    "profile_id": profile["id"],
                    "profile_name": profile["name"],
                    "ontology_file": profile.get("ontology_file") or "",
                    "ontology_subpath": profile.get("ontology_subpath") or "",
                    "shacl_file": filename,
                    "shacl_path": path,
                    "shacl_subpath": subpath,
                    "repo": repo,
                }
                catalog["sources"][source_id] = source
                catalog["by_filename"][filename].append(source)
                family_sources.append(source)
            family_out = {**family, "profiles": prof_records, "sources": family_sources}
            repo_out["families_index"].append(family_out)
        catalog["repositories"].append(repo_out)
    catalog["by_filename"] = dict(catalog["by_filename"])
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--export-shell",
        action="store_true",
        help="Print GraphDB settings as KEY=value for the batch script",
    )
    parser.add_argument(
        "--list-shacl",
        action="store_true",
        help="Print the absolute path of every configured SHACL file",
    )
    args = parser.parse_args()
    if args.export_shell:
        export_shell()
        return 0
    if args.list_shacl:
        list_shacl()
        return 0
    built = build_catalog()
    print(
        f"{len(built['repositories'])} repositories, "
        f"{len(built['sources'])} SHACL source files"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
