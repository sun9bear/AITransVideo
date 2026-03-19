def distribute_text_by_weights(merged_text: str, original_lines: list[str]) -> list[str]:
    if not original_lines:
        return [merged_text] if merged_text else []

    if len(original_lines) == 1:
        return [merged_text]

    clean_text = merged_text.strip()
    if not clean_text:
        return [""] * len(original_lines)

    if len(clean_text) <= len(original_lines):
        return [clean_text[index] if index < len(clean_text) else "" for index in range(len(original_lines))]

    weights = [max(1, len(line.strip())) for line in original_lines]
    total_weight = sum(weights)
    distributed_lines: list[str] = []
    start_index = 0
    cumulative_weight = 0

    for line_index, weight in enumerate(weights):
        if line_index == len(weights) - 1:
            distributed_lines.append(clean_text[start_index:])
            break

        cumulative_weight += weight
        proposed_end = round(len(clean_text) * cumulative_weight / total_weight)
        remaining_lines = len(weights) - line_index - 1
        min_end = start_index + 1
        max_end = len(clean_text) - remaining_lines
        end_index = min(max(proposed_end, min_end), max_end)
        distributed_lines.append(clean_text[start_index:end_index])
        start_index = end_index

    return distributed_lines
