import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.domain.business.models import CatalogExercise

BACKEND_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ACTIONS_PATH = BACKEND_ROOT / "dataset" / "actions.agent.json"
DEFAULT_SCHEMA_PATH = BACKEND_ROOT / "dataset" / "actions.agent.schema.json"


class Catalog:
    # 只读动作目录：按数据集 Schema 校验后投影 §11.7 的九个字段。
    def __init__(self, exercises: list[CatalogExercise]) -> None:
        self._exercises = tuple(exercises)
        self._by_id = {exercise.id: exercise for exercise in exercises}
        self.equipment = frozenset(exercise.equipment for exercise in exercises)
        self.exercise_ids = frozenset(self._by_id)

    @classmethod
    def load(
        cls,
        actions_path: Path = DEFAULT_ACTIONS_PATH,
        schema_path: Path = DEFAULT_SCHEMA_PATH,
    ) -> "Catalog":
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        data = json.loads(Path(actions_path).read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(data)
        return cls(
            [CatalogExercise.model_validate(record) for record in data]
        )

    def get(self, exercise_id: str) -> CatalogExercise | None:
        return self._by_id.get(exercise_id)

    def all(self) -> list[CatalogExercise]:
        return list(self._exercises)

    def search(
        self,
        name: str,
        *,
        equipment: str | None = None,
        body_part: str | None = None,
        target: str | None = None,
        muscle_group: str | None = None,
    ) -> list[CatalogExercise]:
        return [
            exercise
            for exercise in self._exercises
            if name in exercise.name
            and (equipment is None or exercise.equipment == equipment)
            and (body_part is None or exercise.body_part == body_part)
            and (target is None or exercise.target == target)
            and (muscle_group is None or exercise.muscle_group == muscle_group)
        ]

    def __len__(self) -> int:
        return len(self._exercises)
