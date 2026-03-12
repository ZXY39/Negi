# Picture Washing

Use this skill as a process orchestrator only. Keep long prompts in refs files and load them step by step with `read_file`.

## Required Inputs

- `product_image`: product image URL/path/base64
- `reference_images`: one or more reference images
- `ratio`: output ratio such as `1:1`, `3:4`, `4:3`, `16:9`, `9:16`
- `product_description`: product sell points and facts
- `amazon_site`: target site, for example `amazon.com`, `amazon.co.uk`, `amazon.de`

## Optional Inputs (must confirm first)

- `extra_requirements`: extra copy/layout/color constraints
- `style_template_images` or `style_description`

If any required input is missing, stop immediately and return the missing field list.

## Progressive Disclosure Order (must follow)

1. Read `picture-washing/design-specifications.md` and generate a reusable design specification.
2. For each `reference_image`, execute the following sub-steps independently:
- Read `picture-washing/analyze-composition.md` to generate reference composition analysis.
- Read `picture-washing/merge-prompt.md` to merge product facts + design specification + user requirements into a draft prompt.
- Read `picture-washing/compliance-check.md` to produce the final compliant generation prompt.
- Read `picture-washing/picture-washing-skill.md` and call `picture_washing` tool.

Do not preload all refs in one step. Load each file only when the current step needs it.

## Execution and Failure Policy

- Process each reference image independently.
- Do not fail fast for per-image errors; continue with remaining images.
- Final output must include:
- `success_items`: image-level success records with final prompt and returned URLs
- `failed_items`: image-level failure records with failed step and normalized error
- `summary`: total/success/failed counts and retry hints
