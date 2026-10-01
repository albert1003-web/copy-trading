package com.tracker;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Comparator;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.InitializingBean;
import org.springframework.boot.sql.init.dependency.DependsOnDatabaseInitialization;
import org.springframework.core.NestedExceptionUtils;
import org.springframework.core.io.Resource;
import org.springframework.core.io.support.PathMatchingResourcePatternResolver;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;

/**
 * Applies db/migrations/NNN_*.sql above PRAGMA user_version, after schema.sql has run.
 * Mirrors db/__init__.py so the app and the Python pipelines can each upgrade the shared database.
 * "duplicate column name" errors are ignored: the column already exists because schema.sql created it.
 */
@Component
@DependsOnDatabaseInitialization
public class MigrationRunner implements InitializingBean {

    private static final Logger log = LoggerFactory.getLogger(MigrationRunner.class);
    private static final Pattern FILE = Pattern.compile("^(\\d+)_.+\\.sql$");

    private final JdbcTemplate jdbc;

    public MigrationRunner(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public void afterPropertiesSet() throws IOException {
        int current = jdbc.queryForObject("PRAGMA user_version", Integer.class);
        Resource[] files = new PathMatchingResourcePatternResolver().getResources("classpath*:migrations/*.sql");
        Arrays.sort(files, Comparator.comparingInt(MigrationRunner::version));
        for (Resource file : files) {
            int version = version(file);
            if (version <= current) {
                continue;
            }
            for (String statement : statements(file.getContentAsString(StandardCharsets.UTF_8))) {
                try {
                    jdbc.execute(statement);
                } catch (DataAccessException e) {
                    String message = String.valueOf(NestedExceptionUtils.getMostSpecificCause(e).getMessage());
                    if (!message.contains("duplicate column name")) {
                        throw e;
                    }
                }
            }
            jdbc.execute("PRAGMA user_version = " + version);
            log.info("Applied migration {}", file.getFilename());
        }
    }

    private static int version(Resource file) {
        Matcher m = FILE.matcher(String.valueOf(file.getFilename()));
        return m.matches() ? Integer.parseInt(m.group(1)) : -1;
    }

    /** Splits on ";" at line ends, skipping blank and "--" comment lines (migrations keep to that style). */
    static List<String> statements(String sql) {
        List<String> statements = new ArrayList<>();
        StringBuilder buffer = new StringBuilder();
        for (String line : sql.split("\n")) {
            String trimmed = line.strip();
            if (buffer.isEmpty() && (trimmed.isEmpty() || trimmed.startsWith("--"))) {
                continue;
            }
            buffer.append(line).append('\n');
            if (trimmed.endsWith(";")) {
                statements.add(buffer.toString().strip());
                buffer.setLength(0);
            }
        }
        if (!buffer.toString().isBlank()) {
            statements.add(buffer.toString().strip());
        }
        return statements;
    }
}
