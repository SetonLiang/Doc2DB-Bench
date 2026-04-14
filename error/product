[
  {
    "product_id": 135,
    "product_name": "Lock Nut 14",
    "error_type": "Missing ID & Hidden Unit Conversion",
    "description": "原文中仅出现名称 'Lock Nut 14'，完全未提及 ProductID 135（该主键纯属外部数据库映射）。同时，原文记录价格为 54,720，必须结合几十段之外另一句毫无指向性的 'unit of currency for that price is U.S. cents' 隐式除以 100，才能得出真实答案 547.2。"
  },
  {
    "product_id": 383,
    "product_name": "Fender Set - Mountain",
    "error_type": "External ID Dependency",
    "description": "文档中清楚写明了产品名称和价格 21.98，但通篇没有任何文字提及该产品的 ID 是 383。系统无法通过纯文本阅读获取该 ID，属于严重的外部信息依赖。"
  },
  {
    "product_id": 463,
    "product_name": "Touring-3000 Blue, 54",
    "error_type": "Ghost Variants & Implicit Cartesian Product",
    "description": "原文仅提到 'price for the Touring-3000 Blue bike is 742.35'，从未提及有 44, 50, 54, 58 等具体尺码。JSON 标准答案利用外部业务规则，凭空生成了大量文本中不存在的尺码变体记录。"
  },
  {
    "product_id": 71,
    "product_name": "Hex Nut 3",
    "error_type": "Unassigned Surcharges",
    "description": "原文明确写明 Hex Nut 3 的基础价格为 150，但 JSON 标准答案为 156.8。差额 6.8 来源于文中另一处无明确指代对象的规则 'materials surcharge of 6.8 is applied... for certain custom builds'，被标准答案强行暗扣在了该零件上。"
  },
  {
    "product_id": 387,
    "product_name": "Short-Sleeve Classic Jersey, M",
    "error_type": "Data Update Contradiction",
    "description": "文档在前文明确给出其价格为 54.99，但 JSON 真实答案为 53.99。因为在极靠后的段落中，用极其隐晦的方式补充了一句 'the correct price for the jersey is 53.99'，构成了“前文挖坑，后文填土”的跨段落矛盾。"
  },
  {
    "product_id": 284,
    "product_name": "Mountain-200 Silver, 38",
    "error_type": "Data Masking & Omission",
    "description": "原文故意提供了一个错误的干扰项：'incorrectly printed as $2,349.99'，并声称后来被修正，但整篇文档再也没有提供修正后的真实价格。JSON 里的真值 2319.99 属于“无字天书”，无法从文本提取。"
  },
  {
    "product_id": 33,
    "product_name": "Front Derailleur Linkage",
    "error_type": "Implicit Zero-Cost Policy",
    "description": "原文中并没有出现数值 0.0，而是使用业务逻辑术语 'its value is absorbed into the complete derailleur unit' 表达免费或成本转移，模型需要将此文本语义强制转化为数字 0.0。"
  },
  {
    "product_id": 449,
    "product_name": "LL Mountain Frame - Silver, 40",
    "error_type": "Data Masking (Unit Obfuscation)",
    "description": "原文故意不写正常的美元定价，而是将其写为 '26,405 cents'。这是一个刻意的反常识陷阱，要求解析时必须先识别异常单位，再执行数学除法运算得出 264.05。"
  },
  {
    "product_id": 151,
    "product_name": "Lock Washer 2",
    "error_type": "Global Policy Override",
    "description": "原文没有提到它的具体价格，后文有一条孤立的全局规则 'items marked as promotional giveaway has its cost entirely waived'。因为该零件属于 promotional giveaway，所以其价格在 JSON 中被判定为 0.0。"
  },
  {
    "product_id": 300,
    "product_name": "Road-250 Black, 52",
    "error_type": "Semantic Unit Trap",
    "description": "原文描述价格为 '$2.44335 thousand'。它在小数点和单位上同时设下陷阱（以千为单位的小数），如果提取逻辑只抓取数字部分，就会提取成错误的 2.44，实际需转换为 2443.35。"
  }
]
