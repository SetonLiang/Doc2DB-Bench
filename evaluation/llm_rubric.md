# LLM Evaluation Rubrics for Generated Documents

Source: Table 6, "Evaluation rubrics for generated documents (1=worst, 5=best)"

## Dimensions

### 1. Lexical Richness
Measures vocabulary variety, precision, and stylistic maturity.

| Score | Level | Description |
| --- | --- | --- |
| 1 | Repetitive | Minimal variety, robotic repetition |
| 2 | Limited | Simple vocabulary, narrow range |
| 3 | Acceptable | Adequate variety, standard usage |
| 4 | Versatile | Natural synonyms, precise terminology |
| 5 | Sophisticated | Rich nuances, professional mastery |

### 2. Logical Consistency
Measures whether ideas are connected, ordered, and reasoned through coherently.

| Score | Level | Description |
| --- | --- | --- |
| 1 | Fragmented | No connections, random claims |
| 2 | Weak | Loose transitions, vague reasoning |
| 3 | Acceptable | Clear order, basic signaling |
| 4 | Compelling | Strong arguments, coherent progression |
| 5 | Rigorous | Flawless chain, seamless attribution |

### 3. Textual Coherence
Measures readability, flow, and how naturally the text moves between ideas.

| Score | Level | Description |
| --- | --- | --- |
| 1 | Incoherent | Difficult to follow, random jumps |
| 2 | Poor flow | Jarring transitions, disconnected |
| 3 | Acceptable | Some awkward transitions |
| 4 | Smooth | Clear progression, minor issues |
| 5 | Seamless | Natural flow, effortless transitions |


### 4. Genre Fidelity

| Score | Level        | Description                               |
| ----- | ------------ | ----------------------------------------- |
| 1     | Mismatch     | Incorrect genre conventions               |
| 2     | Weak         | Limited or inconsistent genre cues        |
| 3     | Plausible    | Major conventions present, but generic    |
| 4     | Authentic    | Consistent genre-specific conventions     |
| 5     | Professional | Realistic and polished genre presentation |


## Compact JSON-Like Reference

```text
{
  "lexical_richness": {
    "1": "Repetitive: Minimal variety, robotic repetition",
    "2": "Limited: Simple vocabulary, narrow range",
    "3": "Acceptable: Adequate variety, standard usage",
    "4": "Versatile: Natural synonyms, precise terminology",
    "5": "Sophisticated: Rich nuances, professional mastery"
  },
  "logical_consistency": {
    "1": "Fragmented: No connections, random claims",
    "2": "Weak: Loose transitions, vague reasoning",
    "3": "Acceptable: Clear order, basic signaling",
    "4": "Compelling: Strong arguments, coherent progression",
    "5": "Rigorous: Flawless chain, seamless attribution"
  },
  "textual_coherence": {
    "1": "Incoherent: Difficult to follow, random jumps",
    "2": "Poor flow: Jarring transitions, disconnected",
    "3": "Acceptable: Some awkward transitions",
    "4": "Smooth: Clear progression, minor issues",
    "5": "Seamless: Natural flow, effortless transitions"
  }
}
```


