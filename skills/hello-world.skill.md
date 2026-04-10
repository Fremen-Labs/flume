---
name: hello-world
version: 1.0.0
inception: hybrid
ensemble_size: 1
creativity_level: 0
---

## Description
A minimal example skill that generates a personalized greeting. Use this as a
template for creating your own Flume skills.

## Input Schema
```json
{
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "description": "The name to greet"
    },
    "style": {
      "type": "string",
      "enum": ["formal", "casual", "pirate"],
      "description": "Greeting style"
    }
  },
  "required": ["name"]
}
```

## Output Schema
```json
{
  "type": "object",
  "properties": {
    "greeting": {
      "type": "string",
      "description": "The generated greeting message"
    },
    "style_used": {
      "type": "string",
      "description": "The style that was applied"
    }
  },
  "required": ["greeting"]
}
```

## Validation Rules
- Greeting must not be empty
- Greeting must contain the input name

## Prompt Template
Generate a {style} greeting for a person named "{name}".
If no style is specified, use a warm professional tone.
Return a JSON object with "greeting" and "style_used" fields.
