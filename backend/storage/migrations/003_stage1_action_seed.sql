-- 003：Stage 1 S1-03 动作目录精选种子（只 INSERT；不建表、不写用户事实、不预建媒体字段）
-- 依据：stage1.md §5 S1-03「已确认的来源、首批范围与维护方式 / 记录类型 / 负重口径 /
--       动作模式映射」、architecture 03 章 3.1-3.2、design-decisions 架构不变量。
-- 口径：中文标准名用已拍清单原词；别名 = 数据集英文 name；record_type 恰三类；
--       load_convention 只用已拍五种；modes 只用已拍 13 词表；recommendable 恒 0
--       （未通过可推荐检查，不得自动置 1）；active 恒 1（停用只由后续业务置 0）。
-- 首批 24 项清单（2026-09-09 拍定移除原候选「哑铃分腿蹲」：数据集无可靠对应）全部导入；本迁移 24 行。
-- 来源：exercises-dataset（文字数据，attribution 逐行保留）；媒体字段一律不导入。
-- 许可：结构/文字数据来自 exercises-dataset（MIT，© Hasan Emir Yıldırım），副本保留其版权与
--       许可声明；attribution 列为媒体权利声明（© Gym visual），媒体本身不导入。
-- 本文件不含 BEGIN/COMMIT/PRAGMA user_version：由 storage/migrations.py 统一包事务。

INSERT INTO exercises (
    id, standard_name_zh, equipment_variant, record_type, load_convention, unilateral,
    recommendable, active, aliases_json, modes_json, source_ref, attribution, instructions_zh
) VALUES
    -- exercises-dataset:0043 barbell full squat
    ('barbell-back-squat', '杠铃背蹲', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 0, 0, 1, '["barbell full squat"]', '["深蹲"]', 'exercises-dataset:0043', '© Gym visual — https://gymvisual.com/', '站立，双脚分开与肩同宽，脚趾稍微向外。 将杠铃放在上背部，将其放在斜方肌或三角肌后束上。 当你开始降低身体时，启动你的核心并保持胸部挺直。 弯曲膝盖和臀部，向后和向下推臀部，就像坐在椅子上一样。 放低身体，直到大腿与地面平行或稍低于地面。 保持膝盖与脚趾对齐，并将重量放在脚后跟上。 通过脚后跟站起来，伸展臀部和膝盖。 重复所需的重复次数。'),
    -- exercises-dataset:0032 barbell deadlift
    ('barbell-deadlift', '杠铃传统硬拉', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 0, 0, 1, '["barbell deadlift"]', '["髋铰链"]', 'exercises-dataset:0032', '© Gym visual — https://gymvisual.com/', '双脚分开与肩同宽站立，杠铃放在你面前的地面上。 弯曲膝盖并以臀部为铰链，降低躯干，正手握住杠铃，双手分开略宽于肩宽。 当你通过脚后跟将杠铃抬离地面时，保持背部挺直，胸部抬起，伸展臀部和膝盖。 当你站直时，挤压你的臀部并保持你的核心参与。 弯曲臀部和膝盖，将杠铃放回地面，保持背部挺直。 重复所需的重复次数。'),
    -- exercises-dataset:0085 barbell romanian deadlift
    ('barbell-romanian-deadlift', '杠铃罗马尼亚硬拉', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 0, 0, 1, '["barbell romanian deadlift"]', '["髋铰链"]', 'exercises-dataset:0085', '© Gym visual — https://gymvisual.com/', '站立，双脚分开与肩同宽，脚趾指向前方。 正手握住杠铃，双手分开略宽于肩宽。 弯曲臀部，保持背部挺直，膝盖稍微弯曲。 将杠铃向地面降低，使其靠近身体。 当你降低杠铃时，感受腿筋的拉伸。 一旦感觉到腿筋拉伸，就将臀部向前推并站直。 在动作的最高点挤压臀部。 将杠铃放回起始位置，然后重复所需的重复次数。'),
    -- exercises-dataset:0739 sled 45в° leg press
    ('leg-press-45', '45°腿举', 'sled_machine', 'reps_weight', 'plate_loaded_total_excluding_empty', 0, 0, 1, '["sled 45° leg press", "sled 45в° leg press"]', '["深蹲"]', 'exercises-dataset:0739', '© Gym visual — https://gymvisual.com/', '将雪橇机的座椅和脚踏板调整到舒适的位置。 坐在雪橇机上，背部靠在靠背上，双脚与肩同宽放在踏板上。 握住座椅两侧的把手以保持稳定性。 伸展双腿，将脚踏板推离身体，脚后跟保持在脚踏板上。 继续推动，直到双腿几乎完全伸展，但不要锁住膝盖。 在动作的最高点暂停片刻，然后弯曲膝盖，慢慢地将踏板放回到身体的方向。 重复所需的重复次数。'),
    -- exercises-dataset:0410 dumbbell single leg split squat
    ('bulgarian-split-squat', '保加利亚分腿蹲', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 1, 0, 1, '["dumbbell single leg split squat"]', '["深蹲"]', 'exercises-dataset:0410', '© Gym visual — https://gymvisual.com/', '双脚分开与肩同宽站立，每只手各握一个哑铃。 一只脚向前迈出一步，调整双脚的位置，使前脚平放在地面上，后脚抬高在长凳或台阶上。 弯曲前膝盖和臀部，降低身体，保持后膝盖稍微弯曲，后脚跟离开地面。 继续降低，直到大腿前部与地面平行，然后通过前脚跟推回到起始位置。 重复所需的重复次数，然后换腿并重复。'),
    -- exercises-dataset:0025 barbell bench press
    ('barbell-bench-press', '杠铃平板卧推', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 0, 0, 1, '["barbell bench press"]', '["水平推"]', 'exercises-dataset:0025', '© Gym visual — https://gymvisual.com/', '平躺在长凳上，双脚平放在地上，背部紧贴长凳。 正手握住杠铃，握距略宽于肩宽。 将杠铃从架子上提起，并将其直接放在胸部上方，双臂完全伸展。 将杠铃慢慢降低到胸部，保持肘部内收。 当杠铃触及胸部时暂停片刻。 伸展双臂，将杠铃推回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0289 dumbbell bench press
    ('dumbbell-bench-press', '哑铃平板卧推', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 0, 0, 1, '["dumbbell bench press"]', '["水平推"]', 'exercises-dataset:0289', '© Gym visual — https://gymvisual.com/', '平躺在长凳上，双脚平放在地上，背部紧贴长凳。 双手各握一个哑铃，手掌朝前，双臂伸至胸部上方。 慢慢地将哑铃降低到胸部两侧，保持肘部呈 90 度角。 暂停片刻，然后将哑铃推回起始位置，完全伸展双臂。 重复所需的重复次数。'),
    -- exercises-dataset:0314 dumbbell incline bench press
    ('dumbbell-incline-bench-press', '哑铃上斜卧推', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 0, 0, 1, '["dumbbell incline bench press"]', '["水平推", "垂直推"]', 'exercises-dataset:0314', '© Gym visual — https://gymvisual.com/', '设置一个 45 度角的上斜凳。 坐在长凳上，双脚平放在地上，背部紧贴长凳。 双手各握一个哑铃，掌心向前，将哑铃举至肩高。 慢慢地将哑铃降低到胸部两侧，保持肘部呈 90 度角。 将哑铃推回起始位置，充分伸展手臂。 重复所需的重复次数。'),
    -- exercises-dataset:0405 dumbbell seated shoulder press
    ('seated-dumbbell-shoulder-press', '坐姿哑铃肩推', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 0, 0, 1, '["dumbbell seated shoulder press"]', '["垂直推"]', 'exercises-dataset:0405', '© Gym visual — https://gymvisual.com/', '坐在长凳上，每只手各拿一个哑铃，放在大腿上。 将哑铃举至肩高，手掌朝前。 向上推哑铃，直到手臂完全伸过头顶。 在顶部停顿片刻，然后慢慢将哑铃放回肩部高度。 重复所需的重复次数。'),
    -- exercises-dataset:0334 dumbbell lateral raise
    ('dumbbell-lateral-raise', '哑铃侧平举', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 0, 0, 1, '["dumbbell lateral raise"]', '["肩孤立"]', 'exercises-dataset:0334', '© Gym visual — https://gymvisual.com/', '双脚分开与肩同宽站立，双手各握一个哑铃，手掌朝向身体。 保持背部挺直并启动核心肌群。 将手臂向两侧抬起，直到与地板平行，保持肘部稍微弯曲。 在顶部暂停片刻，然后慢慢将手臂放回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0383 dumbbell reverse fly
    ('dumbbell-reverse-fly', '哑铃反向飞鸟', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 0, 0, 1, '["dumbbell reverse fly"]', '["肩孤立"]', 'exercises-dataset:0383', '© Gym visual — https://gymvisual.com/', '双脚分开与肩同宽站立，每只手各握一个哑铃。 稍微弯曲膝盖，髋部向前转动，保持背部挺直。 将双臂伸直在身前，手掌相对。 保持肘部轻微弯曲，将手臂向两侧抬起，直到与地面平行。 在顶部暂停片刻，然后慢慢将手臂放回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0027 barbell bent over row
    ('barbell-bent-over-row', '杠铃俯身划船', 'barbell', 'reps_weight', 'barbell_includes_bar_total', 0, 0, 1, '["barbell bent over row"]', '["水平拉"]', 'exercises-dataset:0027', '© Gym visual — https://gymvisual.com/', '站立，双脚分开与肩同宽，膝盖稍微弯曲。 臀部向前弯曲，同时保持背部挺直、挺胸。 正手握住杠铃，双手间距略宽于肩宽。 通过收缩肩胛骨并挤压背部肌肉，将杠铃拉向下胸部。 在顶部停顿片刻，然后慢慢将杠铃放回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0861 cable seated row
    ('seated-cable-row', '坐姿绳索划船', 'cable', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["cable seated row"]', '["水平拉"]', 'exercises-dataset:0861', '© Gym visual — https://gymvisual.com/', '坐在电缆划船机上，双脚平放在脚踏板上，膝盖稍微弯曲。 正手握住手柄，保持背部挺直，肩膀放松。 将手柄拉向身体，将肩胛骨挤压在一起。 在动作的最高点暂停片刻，然后慢慢松开手柄回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0292 dumbbell one arm bent-over row
    ('one-arm-dumbbell-row', '单臂哑铃划船', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 1, 0, 1, '["dumbbell one arm bent-over row"]', '["水平拉"]', 'exercises-dataset:0292', '© Gym visual — https://gymvisual.com/', '双脚分开与肩同宽站立，一手握住哑铃，手掌朝向身体。 稍微弯曲膝盖，髋部向前转动，保持背部挺直，核心肌群参与。 让哑铃垂直垂向地板，手臂完全伸展。 将哑铃向上拉向胸部，保持肘部靠近身体并将肩胛骨挤压在一起。 在顶部停顿片刻，然后慢慢将哑铃放回起始位置。 重复所需的重复次数，然后换边。'),
    -- exercises-dataset:0198 cable pulldown
    ('lat-pulldown', '高位下拉', 'cable', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["cable pulldown"]', '["垂直拉"]', 'exercises-dataset:0198', '© Gym visual — https://gymvisual.com/', '调整拉索下拉机，使座椅处于舒适的高度并固定护膝。 坐在座位上，背部挺直，双脚平放在地面上。 正手握住电缆杆，握距略宽于肩宽。 稍微向后倾斜并启动你的核心。 将电缆杆向下拉向胸部，将肩胛骨挤压在一起。 在动作底部暂停片刻，然后慢慢松开杠铃回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0652 pull-up
    ('pull-up', '自重引体向上', 'bodyweight', 'reps_bodyweight', NULL, 0, 0, 1, '["pull-up"]', '["垂直拉"]', 'exercises-dataset:0652', '© Gym visual — https://gymvisual.com/', '悬挂在引体向上杆上，手掌背向自己，手臂完全伸展。 启动你的核心并将肩胛骨挤压在一起。 弯曲肘部并将胸部拉向杠铃杆，将身体向上拉向杠铃杆。 在动作的最高点暂停，然后慢慢降低身体回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0599 lever seated leg curl
    ('seated-leg-curl', '坐姿腿弯举', 'leverage_machine', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["lever seated leg curl"]', '["膝屈"]', 'exercises-dataset:0599', '© Gym visual — https://gymvisual.com/', '调整机器以适合您的身体，然后坐在机器上，背部靠在靠背上。 将小腿放在带衬垫的杠杆下方，就在脚踝上方。 抓住机器两侧的手柄以提供支撑。 保持大腿不动，呼气并尽可能向上弯曲双腿。 挤压腿筋时，保持收缩位置短暂停顿。 吸气并缓慢地将控制杆降低回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0585 lever leg extension
    ('leg-extension', '腿屈伸', 'leverage_machine', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["lever leg extension"]', '["膝伸"]', 'exercises-dataset:0585', '© Gym visual — https://gymvisual.com/', '调整机器的座椅高度和靠背以适合您的身体。 坐在机器上，背部靠在靠背上，双脚放在脚垫上。 抓住手柄或侧杆以保持稳定性。 伸直膝盖，举起重物，向前伸展双腿。 在顶部暂停片刻，然后慢慢将重量放回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0605 lever standing calf raise
    ('machine-standing-calf-raise', '器械站姿提踵', 'leverage_machine', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["lever standing calf raise"]', '["小腿（踝跖屈）"]', 'exercises-dataset:0605', '© Gym visual — https://gymvisual.com/', '将机器调整到您的高度，双脚分开与肩同宽站立。 将肩膀放在护垫下方，并握住手柄以保持稳定。 伸展脚踝，将脚后跟尽可能抬高。 在顶部停顿片刻，然后慢慢降低脚后跟回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0294 dumbbell biceps curl
    ('dumbbell-biceps-curl', '哑铃弯举', 'dumbbell', 'reps_weight', 'dumbbell_per_hand', 0, 0, 1, '["dumbbell biceps curl"]', '["肘屈"]', 'exercises-dataset:0294', '© Gym visual — https://gymvisual.com/', '站直，双手各握一个哑铃，手掌朝前，双臂完全伸展。 保持上臂静止，呼气并弯举哑铃，同时收缩二头肌。 继续举重，直到二头肌完全收缩并且哑铃与肩部齐平。 挤压二头肌时，保持收缩位置短暂停顿。 吸气并慢慢开始将哑铃放回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0201 cable pushdown
    ('cable-pushdown', '绳索下压', 'cable', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["cable pushdown"]', '["肘伸"]', 'exercises-dataset:0201', '© Gym visual — https://gymvisual.com/', '将直杆连接到高滑轮电缆机上。 面向机器站立，双脚分开与肩同宽，膝盖稍微弯曲。 正手握住杠铃，双手与肩同宽。 保持肘部靠近身体两侧，上臂保持静止。 呼气并将杠铃向下推，直到肘部完全伸展。 暂停片刻，然后吸气，慢慢地将杠铃返回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0194 cable overhead triceps extension (rope attachment)
    ('cable-overhead-triceps-extension', '绳索过顶臂屈伸', 'cable', 'reps_weight', 'machine_pin_displayed_value', 0, 0, 1, '["cable overhead triceps extension (rope attachment)"]', '["肘伸"]', 'exercises-dataset:0194', '© Gym visual — https://gymvisual.com/', '将绳索连接到高处的缆绳机上。 背对机器站立，双脚与肩同宽。 双手抓住绳子，掌心相对，将双手举过头顶。 保持上臂靠近头部，肘部向前。 弯曲肘部，慢慢将绳子降低到头后。 暂停片刻，然后将手臂伸回到起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0251 chest dip
    ('parallel-bar-dip', '自重双杠臂屈伸', 'bodyweight', 'reps_bodyweight', NULL, 0, 0, 1, '["chest dip"]', '["垂直推", "肘伸"]', 'exercises-dataset:0251', '© Gym visual — https://gymvisual.com/', '将自己置于双杠上，双臂完全伸展，身体伸直。 弯曲肘部，降低身体，直到肩膀低于肘部。 伸直手臂，将自己推回起始位置。 重复所需的重复次数。'),
    -- exercises-dataset:0472 hanging leg raise
    ('hanging-leg-raise', '悬垂举腿', 'bodyweight', 'reps_bodyweight', NULL, 0, 0, 1, '["hanging leg raise"]', '["核心"]', 'exercises-dataset:0472', '© Gym visual — https://gymvisual.com/', '悬挂在引体向上杆上，双臂完全伸展，手掌背向自己。 启动你的核心并将双腿抬起到你面前，保持伸直。 继续抬起，直到双腿与地面平行或达到您可以舒适地达到的高度。 在顶部暂停片刻，然后慢慢将双腿放回起始位置。 重复所需的重复次数。');
