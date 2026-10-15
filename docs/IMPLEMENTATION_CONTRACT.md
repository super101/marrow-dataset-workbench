# First release internal interface

Implementation uses Python 3.11 stdlib HTTP/SQLite plus Pillow, and plain modular browser JavaScript/CSS. No node build required. The semantic product design remains unchanged. All data lives outside the checkout.

## Dataset snapshot (internal)

```
{id,name,revision,created_at,coordinate_mode,source_kind,
 mapping:{version,name,entries:[{code,name,train_name,role,evidence}]},
 rules:{research_permission,permission_evidence,annotation_policy,annotation_evidence,task_definition,historical_exposure},
 images:[{id,asset_path,sha256,pixel_sha256,width,height,
   objects:[{id,raw_code,code,bbox:[xmin,ymin,xmax,ymax],deleted,delete_reason,question,crop_review,crop_note,truncated,difficult}],
   original_objects:[...],review_state,annotations_complete,exclude_reason,note,
   metadata:{patient_id,slide_id,acquisition_batch_id,source_image_id,metadata_evidence,historical_exposure}}],
 relations:[{a,b,kind,evidence}],
 split:{version,confirmed,scope,seed,ratios,assignments:{image_id:train|val|test},groups:{image_id:group_id}}
}
```

asset_path is relative to workspace and is not exposed in API. bbox is zero-based half-open. IDs are safe generated identifiers, original identifiers remain local data. `cell|ignore|unresolved|out_of_scope` mapping roles. out_of_scope requires evidence and task definition; ignore images are excluded. Deleted objects require evidence; unresolved/ignore deletion is blocking unless explicitly documented as `false_annotation: ...`. All normal edits clear split.confirmed, image confirmation; mapping edits clear all image confirmations. revision uses global optimistic concurrency.

## Exporter / grouping module (workbench/exporter.py)

`image_issues(dataset,image) -> list[{kind,detail,object_id?}]`: unknown codes, invalid boxes, questions, ignore semantics, unsafe deletion, duplicate conflicts. Include dynamic duplicate annotation conflicts computed from current live signatures, except excluded duplicate counterparts.

`make_groups(dataset) -> {image_id:group_id}`: shared patient, slide, exact SHA/pixel SHA, shared source ID AND source ID references to image ID, explicit evidence-backed relations. Full transitive closure.

`plan_split(dataset,ratios,seed,scope) -> split dict`: patient/slide completeness validation; component allows limited claims. Three positive ratios sum normalized, >=3 groups; deterministic whole group assignment. `groups`, `assignments`, `scope`, `seed`, `ratios`, `confirmed:false`, `version`.

`preflight(dataset,options,workspace:Path) -> report`: options `{mode:basic|strict,formats:[voc|coco|yolo],classification:bool,crop_margin:float=0,min_crop_size:int=8}`. Returns `{ready,blockers:[str],warnings:[str],included:[image_id],excluded:[{image_id,reasons:[str]}],classes:[{id,name,coco_id}],counts:{train:{images,objects},val:...,test:...},classification:{included:[{image_id,object_id,bbox,split,class_id,flags}],excluded:[{image_id,object_id,reasons}]},reviewed_images,total_images,scope}`. Verify file hashes and dimensions in preflight; research permission verified + evidence; exhaustive policy with evidence OR each image confirmed complete. Strict image state confirmed + annotations_complete. No effective split empty. Class outputs may be empty for detection negatives if other valid classes present. classification only may proceed for crop valid subset but report empty classification splits as blockers when requested.

`build_export(dataset,options,workspace:Path,destination:Path,progress=None) -> report`: new destination only, independent copies (no hardlinks), atomic completion via sibling staging and rename, returns `{path,report,files}`. destination is directory; caller creates zip after success. Crop policies meaningful: strict explicit include required, basic geometric defaults exclude multiple visible main objects by overlap and severe edge/truncation, min-size risks. out_of_scope backgrounds only by fixed task rule. Snapshot manifest/rules/classes/split/excluded/groups/stats/checksums included. Safe internal generated IDs for files, no private source paths/real identity strings in exported files, dataset card warns images need separate privacy review. VOC proper 1-based inclusive, COCO 0-based xywh, YOLO normalized.

## HTTP, same-origin cookie + X-Workbench: 1 on writes

GET /api/bootstrap -> `{datasets:[meta],version,registered_roots,workspace}`
GET /api/datasets -> `{datasets:[meta]}`
POST /api/datasets/demo -> `{job_id}`
POST /api/datasets/import `{kind:workset|voc,source_path,dictionary_path?,name,coordinate_mode:zero|voc}` -> `{job_id}`
GET /api/jobs/{id} -> `{id,status:queued|running|complete|failed,progress,message,result:{dataset_id? | export_id?},error?}`
GET /api/datasets/{id} -> meta
meta: `{id,name,revision,created_at,source_kind,coordinate_mode,mapping,rules,split,summary:{images,unique_images,objects,classes,reviewed,pending,excluded,issue_counts,label_counts:[{code,name,boxes,images,groups}],groups,identity_scope},last_image_id,exports:[{id,created_at,revision,mode}],history?:[...]}`
GET /api/datasets/{id}/images?page=1&limit=24&status=all|pending|confirmed|excluded&issue=...&code=...&q=... -> `{items:[{id,width,height,objects:int,issues:[...],review_state,exclude_reason,thumbnail_url}],total,page,limit}`
GET /api/datasets/{id}/images/{image_id} -> image detail `{...image,issues,asset_url,thumbnail_url}` without asset_path/hash/internal source paths.
PATCH /api/datasets/{id}/images/{image_id} `{expected_revision,objects,note,annotations_complete,exclude_reason,review_state}` -> `{dataset:meta,image:detail}`. `review_state:confirmed` triggers blocking checks and completeness. raw_code cannot be changed. New IDs must start `new-` and backend generates durable UUID. Keep original objects in patches (soft delete). Record deletion reason.
PATCH /api/datasets/{id}/mapping `{expected_revision,mapping:{name,entries}}` -> `{dataset:meta}`
PATCH /api/datasets/{id}/rules `{expected_revision,rules}` -> `{dataset:meta}`
POST /api/datasets/{id}/metadata `{expected_revision,csv_text}` -> `{dataset:meta}`
POST /api/datasets/{id}/relations `{expected_revision,csv_text}` -> `{dataset:meta}`
POST /api/datasets/{id}/split `{expected_revision,scope:patient|slide|component,ratios:[0.7,0.15,0.15],seed:42,confirm:bool,assignments_csv?:str}` -> `{dataset:meta,split}`. POST confirm:false previews without mutation. confirm:true stores split (revision increments) and retains confirmed true at resulting revision.
GET /api/datasets/{id}/history -> `{items:[{at,revision,action,image_id,detail}]}`
POST /api/datasets/{id}/export-check `{expected_revision,...options}` -> preflight
POST /api/datasets/{id}/exports `{expected_revision,...options}` -> `{job_id}`
GET /api/exports/{export_id}/download -> zip
GET /api/datasets/{id}/images/{image_id}/asset?thumbnail=1 OR crop=object_id -> JPEG re-encoded

Errors `{error,message,current_revision?}`, code 400 invalid, 403 origin/path, 404 missing, 409 stale revision. All writes require JSON and X-Workbench: 1, cookie. Frontend request includes credentials same-origin. UI always renders labels/notes as escaped text. No external libraries/fonts/telemetry.
