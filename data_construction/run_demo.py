from __future__ import annotations

import json
from pathlib import Path

from data_construction.src.agents.labeling import LabelingAgent
from data_construction.src.agents.profiler import ProfilerAgent
from data_construction.src.agents.refiner_multi import RefinerAgent
from data_construction.src.agents.serializer import SerializerAgent
from data_construction.src.agents.validator import ValidatorAgent
from data_construction.src.agents.writer import WriterAgent
from data_construction.src.config import get_default_config
from data_construction.src.llm import BaseLLMClient, OpenAILLMClient
from data_construction.src.pipeline import DocumentSynthesisPipeline
from data_construction.src.utils.input_loader import load_database_from_directory, load_single_table_json
from data_construction.src.utils.preprocess_bird_fast import preprocess_database as preprocess_bird_database_fast
from data_construction.src.utils.preprocess_bird_fast import save_preprocessed_data as save_bird_preprocessed_data_fast
from data_construction.src.utils.preprocess_bird import preprocess_database as preprocess_bird_database
from data_construction.src.utils.preprocess_bird import save_preprocessed_data as save_bird_preprocessed_data
from data_construction.src.utils.preprocess_spider import spider_preprocess

# BIRD 预处理（SQLite → schema/tables）的随机种子，与 preprocess_bird_fast.preprocess_database 一致
BIRD_PREPROCESS_RANDOM_SEED = 32


def _is_preprocessed(database_root: Path, tables_subdir: str, schema_filename: str) -> bool:
    return (
        (database_root / "config.py").exists()
        and (database_root / schema_filename).exists()
        and (database_root / tables_subdir).is_dir()
    )


def _resolve_sqlite_for_bird(database_root: Path) -> Path:
    explicit = sorted(database_root.glob("*.sqlite")) + sorted(database_root.glob("*.db"))
    if explicit:
        return explicit[0]

    db_name = database_root.name
    candidates = [
        database_root / f"{db_name}.sqlite",
        database_root / f"{db_name}.db",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"bird sqlite file not found under {database_root}")


def main() -> None:
    app_config = get_default_config()
    llm: BaseLLMClient = OpenAILLMClient(config=app_config.llm)
    
    pipeline = DocumentSynthesisPipeline(
        config=app_config.synthesis,
        labeling_agent=LabelingAgent(llm=llm),
        refiner_agent=RefinerAgent(llm=llm),
        serializer_agent=SerializerAgent(),
        writer_agent=WriterAgent(llm=llm),
        validator_agent=ValidatorAgent(llm=llm),
        profiler_agent=ProfilerAgent(llm=llm),
    )

    if app_config.io.input_mode == "single_table":
        input_data = load_single_table_json(app_config.io.input_path)
    else:
        database_root = Path(app_config.io.database_root)
        parts_lower = {part.lower() for part in database_root.parts}
        already_preprocessed = _is_preprocessed(
            database_root=database_root,
            tables_subdir=app_config.io.tables_subdir,
            schema_filename=app_config.io.schema_filename,
        )

        if not already_preprocessed and "spider" in parts_lower:
            spider_preprocess(
                schema_sql_path=database_root / "schema.sql",
                output_json_path=database_root / app_config.io.schema_filename,
                output_config_path=database_root / "config.py",
                output_tables_dir=database_root / app_config.io.tables_subdir,
                write_config=True,
                write_tables=True,
                max_records_per_table=None
            )

        if not already_preprocessed and "bird" in parts_lower:
            bird_sqlite_path = _resolve_sqlite_for_bird(database_root)
            preprocessed = preprocess_bird_database_fast(
                db_path=str(bird_sqlite_path),
                output_dir=str(database_root),
                db_id=database_root.name,
                max_records_per_table=30,
                random_seed=BIRD_PREPROCESS_RANDOM_SEED,
            )
            if preprocessed is None:
                raise RuntimeError(f"bird preprocess failed for {database_root}")
            save_bird_preprocessed_data_fast(preprocessed, str(database_root))

        input_data = load_database_from_directory(
            database_root=app_config.io.database_root,
            tables_subdir=app_config.io.tables_subdir,
            schema_filename=app_config.io.schema_filename,
        )
    # print(input_data)
    # exit()

    result = pipeline.run(input_data)
    output_path = Path(app_config.io.output_path)
    merged_payload = pipeline.save_result_json(output_path=output_path, result=result)
    print(json.dumps(merged_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
