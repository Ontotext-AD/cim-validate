#!/usr/bin/env python3
"""Configuration and source indexing for the static SHACL cube."""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "dashboard-config.json"

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
        repo["checkout_path"] = (ROOT / str(repo["checkout"])).resolve()
        seen_families: set[str] = set()
        for family in repo.get("families") or []:
            family_id = str(family.get("id") or "")
            if not family_id or family_id in seen_families:
                raise ValueError(
                    f"Family id is missing or duplicated in {repo_id}: {family_id!r}"
                )
            seen_families.add(family_id)
    return raw


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


if __name__ == "__main__":
    built = build_catalog()
    print(
        f"{len(built['repositories'])} repositories, "
        f"{len(built['sources'])} SHACL source files"
    )
