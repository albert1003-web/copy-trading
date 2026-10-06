package com.tracker.repo;

import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * A rule label as common/exit_rules.label writes it, e.g. {@code trailing_stop(pct=0.1)}. Mirrors parse_rule:
 * anything else (an older free-text note) isn't a rule, and the position isn't watched.
 */
record ExitRuleLabel(String name, Map<String, Double> params) {

    static final int MAX_HOLD = 60;  // common/exit_rules.MAX_HOLD

    private static final Pattern LABEL = Pattern.compile("^\\s*([a-z_]+)\\((.*)\\)\\s*$");
    private static final Set<String> RULES = Set.of("fixed_hold", "stop_target", "trailing_stop", "atr_stop", "member_sale");
    private static final Map<String, Double> DEFAULTS = Map.of(
            "fixed_hold.days", 20.0, "stop_target.stop", 0.10, "trailing_stop.pct", 0.10,
            "atr_stop.mult", 3.0, "atr_stop.n", 14.0);

    /** The rule, or null if the text isn't a label. */
    static ExitRuleLabel parse(String text) {
        Matcher m = LABEL.matcher(text == null ? "" : text);
        if (!m.matches() || !RULES.contains(m.group(1))) {
            return null;
        }
        Map<String, Double> params = new HashMap<>();
        for (String part : m.group(2).split(",")) {
            if (part.isBlank()) {
                continue;
            }
            int eq = part.indexOf('=');
            if (eq < 0) {
                return null;
            }
            String value = part.substring(eq + 1).strip();
            try {
                params.put(part.substring(0, eq).strip(), value.equals("None") ? null : Double.valueOf(value));
            } catch (NumberFormatException e) {
                return null;
            }
        }
        return new ExitRuleLabel(m.group(1), params);
    }

    /** A parameter, its dataclass default if left out, or null (e.g. stop_target without a target). */
    Double param(String key) {
        return params.containsKey(key) ? params.get(key) : DEFAULTS.get(name + "." + key);
    }

    /** Trading days after the buy day at which the rule sells at the close. */
    int maxHold() {
        Double days = param(name.equals("fixed_hold") ? "days" : "max_hold");
        return days == null ? MAX_HOLD : days.intValue();
    }
}
