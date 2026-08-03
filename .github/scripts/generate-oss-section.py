#!/usr/bin/env python3
"""
generate-oss-section.py — gera a seção ./oss --live do README via API do GitHub.

Roda como GitHub Action (semanal + push). Consulta a API real do GitHub
com GH_TOKEN (disponível no Actions) e conta as contribuições do autor:

  - PRs abertas por @tneemo no upstream (abertas + merged + total)
  - Reviews formais dados por @tneemo (COMMENTED/APPROVED)
  - Issues abertas por @tneemo
  - Última PR com review nosso

Números vêm da máquina, não da mão — sem "morder a medalha".
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

UPSTREAM = "NousResearch/hermes-agent"
AUTHOR = "tneemo"
TOKEN = os.environ.get("GH_TOKEN", "")
API = "https://api.github.com"


def gh(url: str) -> dict:
    """Chama a API do GitHub (uma página)."""
    req = urllib.request.Request(
        f"{API}{url}",
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "tneemo-profile-oss",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def main() -> int:
    if not TOKEN:
        print("GH_TOKEN ausente (rodando fora do Actions? use gh auth token)", file=sys.stderr)
        return 1

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. PRs do autor no upstream
    try:
        d = gh(f"/search/issues?q=repo:{UPSTREAM}+is:pr+author:{AUTHOR}&per_page=100")
        total_prs = d.get("total_count", 0)
        open_prs = [i for i in d.get("items", []) if i.get("state") == "open"]
        open_count = len(open_prs)
        open_titles = [
            f"[#{i['number']}](https://github.com/{UPSTREAM}/pull/{i['number']}) {i['title'][:40]}"
            for i in open_prs[:3]
        ]
    except Exception as e:
        print(f"erro PRs: {e}", file=sys.stderr)
        total_prs, open_count, open_titles = 0, 0, []

    # 2. Reviews formais do autor
    try:
        d = gh(f"/search/issues?q=repo:{UPSTREAM}+is:pr+reviewed-by:{AUTHOR}&per_page=100")
        reviewed = d.get("total_count", 0)
        recent_reviews = [
            f"[#{i['number']}](https://github.com/{UPSTREAM}/pull/{i['number']})"
            for i in d.get("items", [])[:4]
        ]
    except Exception as e:
        print(f"erro reviews: {e}", file=sys.stderr)
        reviewed, recent_reviews = 0, []

    # 3. Issues abertas do autor
    try:
        d = gh(f"/search/issues?q=repo:{UPSTREAM}+is:issue+author:{AUTHOR}&per_page=50")
        issues = d.get("total_count", 0)
    except Exception as e:
        print(f"erro issues: {e}", file=sys.stderr)
        issues = 0

    # 4. Gera a seção
    open_list = "\n".join(f"  - {t}" for t in open_titles) if open_titles else "  - (nenhuma aberta)"
    review_list = "\n".join(f"  - {r}" for r in recent_reviews) if recent_reviews else "  - (nenhum recente)"

    section = f"""## `./oss --live`

> Números gerados automaticamente pela API do GitHub em {today} —
> sem mão humana, sem medalha mordida. 😄

| Métrica | Valor |
|---|---|
| PRs no upstream ([NousResearch/hermes-agent](https://github.com/{UPSTREAM})) | **{total_prs}** total · **{open_count}** abertas |
| Reviews formais dados | **{reviewed}** |
| Issues abertas por mim | **{issues}** |

**PRs abertas agora:**
{open_list}

**Últimas PRs que revistei:**
{review_list}

---
"""

    # Substitui o bloco entre os marcadores (ou anexa no fim)
    marker_start = "## `./oss --live`"
    marker_end = "---"
    readme = open("README.md", encoding="utf-8").read()
    if marker_start in readme:
        # remove bloco antigo até o próximo --- após o título
        start = readme.index(marker_start)
        end = readme.index(marker_end, start + len(marker_start))
        readme = readme[:start] + section + readme[end + len(marker_end):]
    else:
        # anexa antes de ./contact se existir, senão no fim
        if "## `./contact`" in readme:
            readme = readme.replace(
                "## `./contact`", section + "\n## `./contact`", 1
            )
        else:
            readme = readme.rstrip() + "\n\n" + section
    open("README.md", "w", encoding="utf-8").write(readme)
    print(f"seção OSS atualizada: {total_prs} PRs, {reviewed} reviews, {issues} issues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
