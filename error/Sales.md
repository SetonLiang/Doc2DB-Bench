[
  {
    "sales_id": 6358138,
    "sales_name": "Ann Dull -> Diane Suarez (Half-Finger Gloves, L)",
    "error_type": "Data Masking (Missing Quantity)",
    "description": "原文表述为 'Ann Dull assisted Diane Suarez with an order for Half-Finger Gloves, L.'，文中完全未提及这笔订单的数量。但 JSON 标准答案中赫然出现了精确的 Quantity: 870。这属于纯粹的“无字天书”，无法从文本提取。"
  },
  {
    "sales_id": 4341744,
    "sales_name": "Abraham Bennet -> Gerald Gomez",
    "error_type": "Severe Omission (Missing Product & Quantity)",
    "description": "原文仅一笔带过：'Abraham Bennet facilitated a transaction for the customer Gerald Gomez'。既没有提买了什么产品，也没提数量。但 JSON 答案却精确给出了 ProductID: 215 (Mountain Bike Socks, L) 和 Quantity: 720。"
  },
  {
    "sales_id": 4523948,
    "sales_name": "Stearns MacFeather -> Alexis Hughes",
    "error_type": "Severe Omission (Missing Product & Quantity)",
    "description": "原文描述：'Stearns MacFeather, an employee, facilitated a sale to the customer Alexis Hughes'。文本切断了与具体产品的全部关联，JSON 中却精准给出了 ProductID: 253 (HL Mountain Frame - Silver, 38) 且 Quantity 为 253。"
  },
  {
    "sales_id": 6145373,
    "sales_name": "Heather McBadden -> Kellie Torres",
    "error_type": "Ghost Data Insertion",
    "description": "原文仅提及 'another entry indicates a purchase order involved the employee Heather McBadden and the customer Kellie Torres'。没有任何线索指向具体产品，标准答案却凭空出现了 ProductID: 85 (External Lock Washer 5) 和 Quantity: 590。"
  },
  {
    "sales_id": 6001847,
    "sales_name": "Michel DeFrance -> Stacey Ye",
    "error_type": "Fragmentation & Masking",
    "description": "原文写 'a transaction involving Michel DeFrance and Stacey Ye was recorded'，后来又在另一段补充了这是个 'Standard Volume' 交易，但通篇未提具体商品及精确件数。JSON 强行指定 ProductID 为 148 (Lock Washer 1)，数量为 148。"
  },
  {
    "sales_id": 3917690,
    "sales_name": "Innes del Castillo -> Monica Prasad",
    "error_type": "Data Masking (Blanket Obfuscation)",
    "description": "原文表述为 'a recent shipment handled by Innes del Castillo involved a transaction with Monica Prasad'。完全未提供产品和数量。标准答案利用其隐藏标签 [RI_AC]（可能是 Random Insertion），提取出 ProductID: 28 (Flat Washer 8) 且数量为 533。"
  },
  {
    "sales_id": 454421,
    "sales_name": "Ann Dull -> Aaron Con (LL Mountain Rear Wheel)",
    "error_type": "Implicit Calculation & Entity Delegation",
    "description": "这是一条极少数能在文中找到线索但极具迷惑性的记录。业务员 Ann Dull 是通过隐藏规则 'last name ending in -on... is assigned to Ann' 推导出来的；而数量则变成了一道数学题：'110 multiplied by 3, minus 3'，借此得出真实 Quantity 为 327。"
  },
  {
    "sales_id": 1930345,
    "sales_name": "Employee 15 -> Destiny Flores",
    "error_type": "External Database Dependency (Ghost Entity)",
    "description": "文档中大量出现类似 'Employee 15 is responsible for all customer orders involving touring equipment' 的派单规则。但在 Employees.json 中，ID 15 的员工信息被完全删除（ID从14直接跳到了16）。这是一个典型的对抗性测试掩码，要求跨表强行绑定一个不存在的外键。"
  },
  {
    "sales_id": 1884900,
    "sales_name": "Abraham Bennet -> Joe Lopez (Adjustable Race)",
    "error_type": "Ambiguous Context Definition",
    "description": "原文提到 'The transaction between Abraham Bennet and Joe Lopez involved the sale of an Adjustable Race... total units traded were neither a round number nor a multiple of 10'。这只是对数值特征的模糊描述，JSON 给定的确切答案却是 Quantity: 1。"
  },
  {
    "sales_id": 7438495,
    "sales_name": "Dean Straight -> Marvin Gomez (LL Road Rim)",
    "error_type": "Data Type Coercion Trap",
    "description": "原文为 'first order was for 598.26087 units of the LL Road Rim product'。在 Sales 的 Schema 约束中，Quantity 必须是 INTEGER。必须依靠外部约束将此小数强转并四舍五入为 598（此数据不在您给出的几个样例中，但在原文逻辑中是一个经典的类型转换坑）。"
  }
]
