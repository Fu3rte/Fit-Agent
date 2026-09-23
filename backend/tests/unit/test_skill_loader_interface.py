import pytest

from app.infrastructure.skills.loader import SkillLoader, UnknownSkillError
from config import skills_dir


def test_real_loader_scans_metadata_and_reads_body_separately_from_references() -> None:
    loader = SkillLoader(skills_dir())
    metadata = {item.name: item for item in loader.list_metadata()}

    assert metadata["fitness-knowledge"].description
    body = loader.read_skill("fitness-knowledge")
    reference = loader.read_reference(
        "fitness-knowledge", "references/knowledge-boundaries.md"
    )

    assert "# fitness-knowledge" in body
    assert "knowledge-boundaries.md" in body
    assert reference.path == "references/knowledge-boundaries.md"
    assert reference.text.strip()


def test_unknown_skill_and_reference_path_traversal_fail() -> None:
    loader = SkillLoader(skills_dir())

    with pytest.raises(UnknownSkillError):
        loader.read_skill("unknown-skill")
    with pytest.raises(ValueError):
        loader.read_reference("fitness-knowledge", "references/../../outside.md")
    with pytest.raises(ValueError):
        loader.read_reference("fitness-knowledge", "C:/outside.md")
    with pytest.raises(ValueError):
        loader.read_reference("fitness-knowledge", "SKILL.md")
