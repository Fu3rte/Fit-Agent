# Skills评测记录

## 指标口径补充：skills-live-20261022-01

数据来源：`backend/temp/skills-evaluation/skills-live-20261022-01/summary.json`。冻结身份：`a4deac43f1c56b8989b595a96925d055b61a77a707b05a7edb61f23c70e7b019`。86份完成回答均已评分；4个生成失败终态保留。

### 专业通过率的两种口径

- 端到端专业通过率：专业题最终通过数 / 原定24道专业题，包含运行失败。
- 成功运行专业通过率：专业题最终通过数 / execution=completed的专业题数，仅排除运行失败。裁判判为fail或unknown不因此剔除。
- 最终通过：`skills_professional_requirements_v2=pass`且`skills_critical_errors_v2=absent`。

| 配置 | 专业通过数 | 专业目标 | 专业运行成功数 | 排除的运行失败数 | 端到端专业通过率 | 成功运行专业通过率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A0 无Skill | 20 | 24 | 23 | 1 | 83.33%（20/24） | 86.96%（20/23） |
| A1 自动按需读取 | 21 | 24 | 22 | 2 | 87.50%（21/24） | 95.45%（21/22） |
| A2 预选相关知识 | 23 | 24 | 23 | 1 | 95.83%（23/24） | 100.00%（23/23） |

| 对照 | 端到端专业通过率差值 | 成功运行专业通过率差值 |
| --- | ---: | ---: |
| A1 − A0 | +4.17个百分点 | +8.50个百分点 |
| A2 − A1 | +8.33个百分点 | +4.55个百分点 |

差值使用未舍入比例计算。运行失败：013在三组均超时，014在A1为execution_failed。不同组成功运行的题目集合不完全相同，条件通过率差值存在样本选择影响。

### 读取与其他结果

A1任务层触发Precision=100%（23/23），Recall=95.83%（23/24），负例误触发率=0%（0/6）；主题正文加载比例=91.67%（22/24）。16题存在未登记路径待相关性复核；任务层Precision不代表全部主题读取均已确认相关。

三组负例任务通过率均为100%（6/6）。86份已评回答未发现清单内严重错误；4个运行失败样本未评分。A1/A0共同已评分28对，2胜、0负、26平；A2/A1共同已评分28对，1胜、0负、27平。

### 使用边界

本轮30题、三配置、每题每配置单次；生成与裁判均为glm-5.3-flash，参考身份为diagnostic_draft_reference。上述比例是该轮候选参考下的评测结果，未经独立专家/科学金标核验。简历引用95.45%时须注明成功运行专业样本口径，保留21/22及基线20/23的可追溯依据。A2为预选知识诊断配置。

以下保留既有按marker追加的运行记录与报告摘要。


<!-- 0d21b4e0ce53f081608562f713f5c43bff71c7c1a8e868b369cdf6c385e06aa4 -->
## skills-live-20260702-01

报告：backend/temp/skills-evaluation/skills-live-20260702-01/report.md

状态：blocked / incomplete。90目标中4终结（A0完成1、A1失败2、A2完成1），86未执行；每组固定24专业/6负例。工具续轮请求级失败复现后已暂停，底层原因unknown。A2内部失败后恢复并completed。评分0/90：缺现有LangSmith认证环境及真实decision-model-preview网页Evaluator/输出协议/feedback_source绑定，未sync/submit/collect，无本轮云端链接。专业通过率、严重错误率、负例通过率、A1触发比例与配对评分差异均未计算。

生成模型：openai-completions / commandcode / minimax/minimax-m3.1-flash:free，生产参数，240秒预算。17真实观测请求记录（10失败记录usage缺失），已观测input=63131、output=3899、cache_read=46559、reasoning=2000、服务total_tokens=113589；完整用量、精确HTTP请求量、生成/裁判费用与裁判额度未知。无额外API探测。A1入口读取2题、主题读取1题；专业未知22、负例未知6，不将缺事件计为FN。

全部90状态和逐题差异索引在报告；冻结身份在backend/temp/skills-generation/skills-live-20260702-01/run.json；请求SHA256/角色/配对核对及隔离数据库诊断在backend/temp/skills-evaluation/skills-live-20260702-01/preflight-and-diagnostics.json。执行时间以样本UTC为准（2026-10-22），run_id为不透明标签。参考维持diagnostic_draft_reference。下面report_sha256绑定包含执行状态补记的最终MD。

```json
{
  "run_id": "skills-live-20260702-01",
  "identity_sha256": "fea0ced29e64d7f8ae9c84f5b16712ff264e0e9c7fa2df5619b8e0f70e9e7dca",
  "source_sha256": "283831a5c1c4ac1e6f39a06485ab17263032cb254cf246c2012a25df144ea22a",
  "reference_status": "diagnostic_draft_reference",
  "groups": {
    "A0": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 1,
        "not_run": 29
      },
      "scoring": {
        "missing_feedback": 30
      },
      "professional_pass_rate": null,
      "negative_pass_rate": null,
      "confirmed_critical_error_rate": null,
      "critical_error_unknown": 24,
      "scores": {
        "skills_professional_requirements_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 1,
          "missing": 29,
          "median": 14216,
          "p95": 14216
        },
        "output_tokens": {
          "observed": 1,
          "missing": 29,
          "median": 1587,
          "p95": 1587
        },
        "requests": {
          "observed": 1,
          "missing": 29,
          "median": 1,
          "p95": 1
        },
        "read": {
          "observed": 1,
          "missing": 29,
          "median": 0,
          "p95": 0
        },
        "seconds": {
          "observed": 1,
          "missing": 29,
          "median": 33.26608,
          "p95": 33.26608
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown"
    },
    "A1": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "failed": 2,
        "not_run": 28
      },
      "scoring": {
        "missing_feedback": 30
      },
      "professional_pass_rate": null,
      "negative_pass_rate": null,
      "confirmed_critical_error_rate": null,
      "critical_error_unknown": 24,
      "scores": {
        "skills_professional_requirements_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 2,
          "missing": 28,
          "median": 6.0,
          "p95": 8
        },
        "read": {
          "observed": 2,
          "missing": 28,
          "median": 3.0,
          "p95": 5
        },
        "seconds": {
          "observed": 2,
          "missing": 28,
          "median": 32.7669705,
          "p95": 39.78179
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown",
      "natural_trigger": {
        "tp": 2,
        "fp": 0,
        "fn": 0,
        "precision": null,
        "recall": null,
        "negative_false_trigger_rate": null,
        "observed_cases": 2,
        "professional_observed_cases": 2,
        "professional_unknown_cases": 22,
        "negative_observed_cases": 0,
        "negative_unknown_cases": 6,
        "entry_cases": 2,
        "topic_cases": 1,
        "registered_related_cases": 1,
        "related_including_reviewed_equivalent_cases": 1,
        "unrelated_read_cases": 0,
        "unobserved_cases": 28,
        "unregistered_read_cases_pending_review": 1
      }
    },
    "A2": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 1,
        "not_run": 29
      },
      "scoring": {
        "missing_feedback": 30
      },
      "professional_pass_rate": null,
      "negative_pass_rate": null,
      "confirmed_critical_error_rate": null,
      "critical_error_unknown": 24,
      "scores": {
        "skills_professional_requirements_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 1,
          "missing": 29,
          "median": 4,
          "p95": 4
        },
        "read": {
          "observed": 1,
          "missing": 29,
          "median": 1,
          "p95": 1
        },
        "seconds": {
          "observed": 1,
          "missing": 29,
          "median": 76.665549,
          "p95": 76.665549
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown"
    }
  },
  "comparisons": {
    "A1−A0": {
      "professional_pass_percentage_points": null,
      "paired_score_differences": {
        "skills_professional_requirements_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      }
    },
    "A2−A1": {
      "professional_pass_percentage_points": null,
      "paired_score_differences": {
        "skills_professional_requirements_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v1": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      }
    }
  },
  "report_sha256": "9561397d6f39641e9cb389c348b08739fccdf92c4fcd013024b6b2b50e591838"
}
```

<!-- e8031f78c537e8d79e58db6ea20df142220511b136075e66df43e00ef5dc16c1 -->
## skills-live-20261022-01

报告：backend/temp/skills-evaluation/skills-live-20261022-01/report.md

```json
{
  "run_id": "skills-live-20261022-01",
  "identity_sha256": "a4deac43f1c56b8989b595a96925d055b61a77a707b05a7edb61f23c70e7b019",
  "source_sha256": "0e6a125e2dd8f806b9ced62c4ac43389dbaee7b07eb03e535e4fa17705678ec2",
  "reference_status": "diagnostic_draft_reference",
  "groups": {
    "A0": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 29,
        "failed": 1
      },
      "scoring": {
        "scored": 19,
        "missing_feedback": 11
      },
      "scoring_coverage": 19,
      "professional_pass_rate": 0.6666666666666666,
      "negative_pass_rate": 0.0,
      "confirmed_critical_error_rate": 0.0,
      "critical_error_unknown": 5,
      "categories": {
        "skills_professional_requirements_v2": {
          "pass": 16,
          "fail": 3,
          "missing_feedback": 11
        },
        "skills_critical_errors_v2": {
          "absent": 19,
          "missing_feedback": 11
        }
      },
      "scores": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 14407,
          "p95": 16288
        },
        "output_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 1258,
          "p95": 3716
        },
        "requests": {
          "observed": 30,
          "missing": 0,
          "median": 1.0,
          "p95": 3
        },
        "read": {
          "observed": 30,
          "missing": 0,
          "median": 0.0,
          "p95": 0
        },
        "seconds": {
          "observed": 30,
          "missing": 0,
          "median": 43.139094,
          "p95": 166.015738
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown"
    },
    "A1": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 28,
        "failed": 2
      },
      "scoring": {
        "scored": 5,
        "missing_feedback": 25
      },
      "scoring_coverage": 5,
      "professional_pass_rate": 0.20833333333333334,
      "negative_pass_rate": 0.0,
      "confirmed_critical_error_rate": 0.0,
      "critical_error_unknown": 19,
      "categories": {
        "skills_professional_requirements_v2": {
          "pass": 5,
          "missing_feedback": 25
        },
        "skills_critical_errors_v2": {
          "absent": 5,
          "missing_feedback": 25
        }
      },
      "scores": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 28,
          "missing": 2,
          "median": 16210.0,
          "p95": 24872
        },
        "output_tokens": {
          "observed": 28,
          "missing": 2,
          "median": 1988.5,
          "p95": 4527
        },
        "requests": {
          "observed": 30,
          "missing": 0,
          "median": 3.0,
          "p95": 4
        },
        "read": {
          "observed": 30,
          "missing": 0,
          "median": 3.0,
          "p95": 5
        },
        "seconds": {
          "observed": 30,
          "missing": 0,
          "median": 76.9296525,
          "p95": 143.526678
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown",
      "natural_trigger": {
        "tp": 23,
        "fp": 0,
        "fn": 1,
        "precision": 1.0,
        "recall": 0.9583333333333334,
        "negative_false_trigger_rate": 0.0,
        "observed_cases": 30,
        "professional_observed_cases": 24,
        "professional_unknown_cases": 0,
        "negative_observed_cases": 6,
        "negative_unknown_cases": 0,
        "entry_cases": 23,
        "topic_cases": 22,
        "registered_related_cases": 22,
        "related_including_reviewed_equivalent_cases": 22,
        "unrelated_read_cases": 0,
        "unobserved_cases": 0,
        "unregistered_read_cases_pending_review": 16
      }
    },
    "A2": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 29,
        "failed": 1
      },
      "scoring": {
        "scored": 2,
        "missing_feedback": 28
      },
      "scoring_coverage": 2,
      "professional_pass_rate": 0.08333333333333333,
      "negative_pass_rate": 0.0,
      "confirmed_critical_error_rate": 0.0,
      "critical_error_unknown": 22,
      "categories": {
        "skills_professional_requirements_v2": {
          "pass": 2,
          "missing_feedback": 28
        },
        "skills_critical_errors_v2": {
          "absent": 2,
          "missing_feedback": 28
        }
      },
      "scores": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 16021,
          "p95": 20132
        },
        "output_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 1447,
          "p95": 2602
        },
        "requests": {
          "observed": 30,
          "missing": 0,
          "median": 1.0,
          "p95": 3
        },
        "read": {
          "observed": 30,
          "missing": 0,
          "median": 0.0,
          "p95": 0
        },
        "seconds": {
          "observed": 30,
          "missing": 0,
          "median": 50.589867999999996,
          "p95": 85.380116
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown"
    }
  },
  "comparisons": {
    "A1−A0": {
      "professional_pass_percentage_points": -45.83333333333333,
      "paired_score_differences": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      }
    },
    "A2−A1": {
      "professional_pass_percentage_points": -12.5,
      "paired_score_differences": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      }
    }
  },
  "report_sha256": "52e315ff8f0e3adcb26603975dfd297393598000bbaa39c9dc7656a13faa9de3"
}
```

<!-- 6ea88b761a9859016c01c6e65f801ebcf61e72be5460a83bead194f3da2efbf8 -->
## skills-live-20261022-01

报告：backend/temp/skills-evaluation/skills-live-20261022-01/report.md

```json
{
  "run_id": "skills-live-20261022-01",
  "identity_sha256": "a4deac43f1c56b8989b595a96925d055b61a77a707b05a7edb61f23c70e7b019",
  "source_sha256": "0e6a125e2dd8f806b9ced62c4ac43389dbaee7b07eb03e535e4fa17705678ec2",
  "reference_status": "diagnostic_draft_reference",
  "groups": {
    "A0": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 29,
        "failed": 1
      },
      "scoring": {
        "scored": 29,
        "missing_feedback": 1
      },
      "scoring_coverage": 29,
      "professional_pass_rate": 0.8333333333333334,
      "negative_pass_rate": 1.0,
      "confirmed_critical_error_rate": 0.0,
      "critical_error_unknown": 1,
      "categories": {
        "skills_professional_requirements_v2": {
          "pass": 26,
          "fail": 3,
          "missing_feedback": 1
        },
        "skills_critical_errors_v2": {
          "absent": 29,
          "missing_feedback": 1
        }
      },
      "scores": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 14407,
          "p95": 16288
        },
        "output_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 1258,
          "p95": 3716
        },
        "requests": {
          "observed": 30,
          "missing": 0,
          "median": 1.0,
          "p95": 3
        },
        "read": {
          "observed": 30,
          "missing": 0,
          "median": 0.0,
          "p95": 0
        },
        "seconds": {
          "observed": 30,
          "missing": 0,
          "median": 43.139094,
          "p95": 166.015738
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown"
    },
    "A1": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 28,
        "failed": 2
      },
      "scoring": {
        "scored": 28,
        "missing_feedback": 2
      },
      "scoring_coverage": 28,
      "professional_pass_rate": 0.875,
      "negative_pass_rate": 1.0,
      "confirmed_critical_error_rate": 0.0,
      "critical_error_unknown": 2,
      "categories": {
        "skills_professional_requirements_v2": {
          "pass": 27,
          "fail": 1,
          "missing_feedback": 2
        },
        "skills_critical_errors_v2": {
          "absent": 28,
          "missing_feedback": 2
        }
      },
      "scores": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 28,
          "missing": 2,
          "median": 16210.0,
          "p95": 24872
        },
        "output_tokens": {
          "observed": 28,
          "missing": 2,
          "median": 1988.5,
          "p95": 4527
        },
        "requests": {
          "observed": 30,
          "missing": 0,
          "median": 3.0,
          "p95": 4
        },
        "read": {
          "observed": 30,
          "missing": 0,
          "median": 3.0,
          "p95": 5
        },
        "seconds": {
          "observed": 30,
          "missing": 0,
          "median": 76.9296525,
          "p95": 143.526678
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown",
      "natural_trigger": {
        "tp": 23,
        "fp": 0,
        "fn": 1,
        "precision": 1.0,
        "recall": 0.9583333333333334,
        "negative_false_trigger_rate": 0.0,
        "observed_cases": 30,
        "professional_observed_cases": 24,
        "professional_unknown_cases": 0,
        "negative_observed_cases": 6,
        "negative_unknown_cases": 0,
        "entry_cases": 23,
        "topic_cases": 22,
        "registered_related_cases": 22,
        "related_including_reviewed_equivalent_cases": 22,
        "unrelated_read_cases": 0,
        "unobserved_cases": 0,
        "unregistered_read_cases_pending_review": 16
      }
    },
    "A2": {
      "target": 30,
      "professional_target": 24,
      "negative_target": 6,
      "execution": {
        "completed": 29,
        "failed": 1
      },
      "scoring": {
        "scored": 29,
        "missing_feedback": 1
      },
      "scoring_coverage": 29,
      "professional_pass_rate": 0.9583333333333334,
      "negative_pass_rate": 1.0,
      "confirmed_critical_error_rate": 0.0,
      "critical_error_unknown": 1,
      "categories": {
        "skills_professional_requirements_v2": {
          "pass": 29,
          "missing_feedback": 1
        },
        "skills_critical_errors_v2": {
          "absent": 29,
          "missing_feedback": 1
        }
      },
      "scores": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "generation_usage": {
        "input_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 16021,
          "p95": 20132
        },
        "output_tokens": {
          "observed": 29,
          "missing": 1,
          "median": 1447,
          "p95": 2602
        },
        "requests": {
          "observed": 30,
          "missing": 0,
          "median": 1.0,
          "p95": 3
        },
        "read": {
          "observed": 30,
          "missing": 0,
          "median": 0.0,
          "p95": 0
        },
        "seconds": {
          "observed": 30,
          "missing": 0,
          "median": 50.589867999999996,
          "p95": 85.380116
        }
      },
      "scoring_usage": {
        "status": "unknown: feedback contract has no verified usage mapping",
        "input_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "output_tokens": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "requests": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "read": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "seconds": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      },
      "cost": "unknown"
    }
  },
  "comparisons": {
    "A1−A0": {
      "professional_pass_percentage_points": 4.1666666666666625,
      "paired_score_differences": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      }
    },
    "A2−A1": {
      "professional_pass_percentage_points": 8.333333333333337,
      "paired_score_differences": {
        "skills_professional_requirements_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        },
        "skills_critical_errors_v2": {
          "observed": 0,
          "missing": 30,
          "median": null,
          "p95": null
        }
      }
    }
  },
  "report_sha256": "8bf350137c642d498636548f52168b112492837655e9a6a78be5a51f2cd2b154"
}
```
