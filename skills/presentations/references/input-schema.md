# PPTX workflow input schema

`create` accepts a workspace-relative JSON file with this shape:

```json
{
  "title": "Course report",
  "slides": [
    {
      "title": "Research question",
      "body": "One concise explanation.",
      "bullets": ["Background", "Objective"],
      "image": "artifacts/figure.png"
    }
  ]
}
```

`title` and `slides` are required. `title` and every slide `title` are non-empty strings; `slides` may be an empty array. `body` is an optional non-empty string; `bullets` is an optional array of non-empty strings. `image` is optional and must be a workspace-relative file path. Do not use absolute paths or `..` traversal.

The command creates a 16:9 title slide plus one slide per `slides` element.
