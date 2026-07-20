# ADR-002: Canonical normalized annotation JSON

- **Status:** Accepted (2026-07-16)
- **Decision:** All imports create a per-sample normalized annotation JSON object in object storage. PostgreSQL retains only its lightweight `AnnotationIndex`, `SampleClassIndex`, label/keypoint definitions, and a reference to the normalized object.
- **Context:** YOLO, COCO, LabelMe, and Pascal VOC encode classes and geometry differently. Keeping UI and export logic coupled to each original file would make migration and feature work brittle.
- **Consequences:** Raw source annotations are retained for traceability, but preview overlays, quality checks, and export adapters consume the normalized schema. Storage remains provider-portable because objects are referenced by bucket/key. New formats require a parser adapter, not new UI/database columns.
