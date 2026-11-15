# PDF workflow JSON input

`create --spec` consumes one JSON object:

```json
{
  "title": "实验报告",
  "subtitle": "可选副标题",
  "sections": [
    {
      "heading": "结论",
      "paragraphs": ["段落一", "段落二"],
      "bullets": ["要点一", "要点二"],
      "table": [["列 A", "列 B"], ["值 A", "值 B"]]
    }
  ]
}
```

`title` is required and must be a non-empty string. `subtitle` is optional. Each section may contain a string `heading`, arrays of string `paragraphs` and `bullets`, and a `table` array of rows. Each table cell must be a string or number.

`fill --values` consumes an object whose keys are existing PDF AcroForm field names and whose values are field values:

```json
{"student_name": "Ada", "student_id": "20260001"}
```

Unknown field names fail with `PDF_FIELD_NOT_FOUND`. Encrypted PDFs cannot be filled.
