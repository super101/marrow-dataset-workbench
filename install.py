"""Install this skill and three Codex role configurations without replacing existing files."""
import argparse
import datetime
import os
from pathlib import Path
import shutil
import tomllib

ROLES = {
    'marrow-data-curator': 'Preserve and organize image/XML sources, worksets and provenance.',
    'marrow-label-reviewer': 'Audit dictionary mapping, boxes, ignore regions and annotation disagreements.',
    'marrow-grouping-viewer': 'Prepare verified identity grouping, leakage review and local dataset demonstrations.',
}


def install(repo, codex_home):
    config = codex_home / 'config.toml'
    original = config.read_text(encoding='utf-8') if config.exists() else ''
    existing = tomllib.loads(original)
    # Never replace a user's skill or role definition during installation.
    skill = codex_home / 'skills' / 'marrow-dataset-intake'
    if skill.exists():
        raise ValueError('Skill already installed. Review/diff it before updating.')
    for role in ROLES:
        if role in existing.get('agents', {}) or (codex_home / 'agents' / (role + '.toml')).exists():
            raise ValueError('Role already exists: ' + role)
    additions = '\n# Marrow Dataset Workbench roles\n'
    for role, description in ROLES.items():
        additions += f'\n[agents.{role}]\ndescription = "{description}"\nconfig_file = "agents/{role}.toml"\n'
    result = original.rstrip() + '\n' + additions
    tomllib.loads(result)
    skill.parent.mkdir(parents=True, exist_ok=True)
    role_folder = codex_home / 'agents'
    role_folder.mkdir(parents=True, exist_ok=True)
    shutil.copytree(repo / 'skills' / 'marrow-dataset-intake', skill,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for role in ROLES:
        shutil.copyfile(repo / 'agents' / (role + '.toml'), role_folder / (role + '.toml'))
    if config.exists():
        suffix = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')
        shutil.copyfile(config, config.with_name('config.toml.before-marrow-' + suffix + '.bak'))
    config.write_text(result, encoding='utf-8')
    print('Installed marrow-dataset-intake and 3 Codex roles. Existing configuration backed up; restart/reopen Codex to discover roles.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.codex_home)
