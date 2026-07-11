"""从 CHANGELOG.md 提取指定版本的 release notes。"""

import re
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: generate_release_notes.py <tag>", file=sys.stderr)
        return 1

    tag = sys.argv[1]
    version = tag.lstrip("vV")

    with open("CHANGELOG.md", "r", encoding="utf-8") as f:
        content = f.read()

    # 匹配 ## vX.Y.Z 到下一个 ## v 或文件末尾之间的内容
    pattern = rf"## v{re.escape(version)}\n(.*?)(?:\n## v|\Z)"
    match = re.search(pattern, content, re.S)

    if not match:
        print(f"Version {version} not found in CHANGELOG.md", file=sys.stderr)
        return 1

    notes = match.group(1).strip()
    if not notes:
        print(f"No notes found for version {version}", file=sys.stderr)
        return 1

    output = (
        f"## RemoveBlack {tag} 更新内容\n\n"
        f"{notes}\n\n"
        f"### 下载说明\n\n"
        f"- RemoveBlack.exe：GUI 主程序，双击运行\n"
        f"- RemoveBlack-{tag}.exe：带版本号副本\n\n"
        f"> 本 release 的 exe 由 GitHub Actions 云端构建生成。\n"
    )

    with open("release_notes.md", "w", encoding="utf-8") as f:
        f.write(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
