from app.application.business import retrieval_text as rt
from app.application.business.catalog import Catalog


def check_normalize() -> None:
    assert rt.normalize("ＡＢＣ") == "abc"
    assert rt.normalize("３/４") == "3/4"
    assert rt.normalize("  Barbell\t\nBench   Press ") == "barbell bench press"
    assert rt.normalize("杠铃卧推") == "杠铃卧推"
    assert rt.normalize("Dumbbell 卧推") == "dumbbell 卧推"
    assert rt.normalize("哑铃（上斜）") == rt.normalize("哑铃(上斜)")
    assert rt.normalize("") == ""


def check_query_canonical(mappings: dict) -> None:
    assert rt.query_canonical(mappings, "equipment", "哑铃") == {"dumbbell"}
    assert rt.query_canonical(mappings, "equipment", "自重") == {"body weight"}
    assert rt.query_canonical(mappings, "equipment", "BODY WEIGHT") == {"body weight"}
    assert rt.query_canonical(mappings, "body_part", "背部") == {"back"}
    assert rt.query_canonical(mappings, "body_part", "背") == {"back"}
    assert rt.query_canonical(mappings, "target", "胸肌") == {"pectorals"}
    assert rt.query_canonical(mappings, "muscle_group", "背阔肌") == {"latissimus dorsi", "lats"}
    # 同义词组与跨 canonical 共享词返回完整集合，不丢失同组值。
    assert rt.query_canonical(mappings, "muscle_group", "斜方肌") == {"trapezius", "traps"}
    assert rt.query_canonical(mappings, "equipment", "器械") == {"leverage machine", "sled machine"}
    assert rt.query_canonical(mappings, "equipment", "不存在的器械") == set()


def check_unified_text(catalog: Catalog, mappings: dict) -> None:
    exercise = catalog.get("0001")
    assert exercise is not None and exercise.id == "0001", "前导零 ID 必须保持字符串"
    supplement = {
        "english_name": {"text": "3/4 sit-up", "review_status": "approved"},
        "aliases": [
            {"text": "四分之三卷腹", "review_status": "candidate"},
            {"text": "3/4 crunch", "review_status": "approved"},
        ],
    }
    text = rt.build_unified_text(exercise, supplement, mappings)
    assert text == rt.build_unified_text(exercise, supplement, mappings), "编码必须确定性"
    segments = text.split("\n")
    assert segments[0] == rt.normalize(exercise.name)
    # 字段顺序：名称 → 别名 → 英文名 → 器械 → 步骤；缺失字段不产生空占位。
    assert segments[1] == rt.normalize("3/4 crunch")
    assert segments[2] == rt.normalize("3/4 sit-up")
    assert segments.index(rt.normalize("body weight 徒手")) == 3
    assert segments[-1] == rt.normalize(" ".join(exercise.steps.en))
    assert rt.normalize("3/4 crunch") in text
    assert "四分之三卷腹" not in text, "未审阅别名默认不得进入文本"
    candidate = rt.build_unified_text(exercise, supplement, mappings, include_unreviewed=True)
    assert "四分之三卷腹" in candidate
    # 无补充条目时不产生英文名与别名段。
    bare = rt.build_unified_text(exercise, None, mappings).split("\n")
    assert rt.normalize("3/4 sit-up") not in bare and len(bare) == len(segments) - 2


def check_review_gate(catalog: Catalog, mappings: dict) -> None:
    # 真实审阅状态门禁：英文名称与别名同样只放行 approved，candidate/rejected 均排除。
    exercise = catalog.get("0001")
    assert exercise is not None
    for status in ("candidate", "rejected"):
        supplement = {
            "english_name": {"text": "unreviewed sit-up", "review_status": status},
            "aliases": [
                {"text": "未审阅卷腹", "review_status": status},
                {"text": "已审阅卷腹", "review_status": "approved"},
            ],
        }
        text = rt.build_unified_text(exercise, supplement, mappings)
        assert "unreviewed sit-up" not in text, f"{status} 英文名称不得进入统一文本"
        assert "未审阅卷腹" not in text, f"{status} 别名不得进入统一文本"
        assert rt.normalize("已审阅卷腹") in text
        opened = rt.build_unified_text(exercise, supplement, mappings, include_unreviewed=True)
        assert "unreviewed sit-up" in opened
        assert "未审阅卷腹" in opened
    assert rt.approved_english_name(None) == ""
    assert rt.approved_aliases(None) == []


def check_alias_conflict(catalog: Catalog, mappings: dict) -> None:
    # 同一别名对应两个具体变式：两条文本都保留该别名，且 ID 关系不丢失。
    left = catalog.get("0088")
    right = catalog.get("1371")
    assert left.name == right.name == "杠铃坐姿提踵", "重复名称应保留独立 ID"
    assert left.id == "0088" and right.id == "1371"
    alias = {"text": "坐姿提踵", "review_status": "approved"}
    supplement = {"english_name": {"text": "barbell seated calf raise", "review_status": "approved"}, "aliases": [alias]}
    left_text = rt.build_unified_text(left, supplement, mappings)
    right_text = rt.build_unified_text(right, supplement, mappings)
    assert rt.normalize("坐姿提踵") in left_text and rt.normalize("坐姿提踵") in right_text
    assert left_text != right_text or left.steps == right.steps


def check_provenance(mappings: dict, document: dict) -> None:
    info = rt.provenance(mappings, document)
    assert info["encoding_rule_version"] == rt.ENCODING_RULE_VERSION
    assert info["field_order"] == list(rt.FIELD_ORDER)
    assert len(info["catalog_sha256"]) == 64
    assert info["entry_count"] == len(document["entries"])


def check() -> None:
    catalog = Catalog.load()
    mappings = rt.load_mappings()
    document = rt.load_supplements()
    check_normalize()
    check_query_canonical(mappings)
    check_unified_text(catalog, mappings)
    check_review_gate(catalog, mappings)
    check_alias_conflict(catalog, mappings)
    check_provenance(mappings, document)
    print("统一文本编码：规范化、字段顺序、别名冲突与前导零检查通过")


if __name__ == "__main__":
    check()
