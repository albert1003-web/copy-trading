package com.tracker.api;

import java.util.Map;

import org.sqlite.SQLiteException;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.UncategorizedSQLException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** Maps database constraint failures (e.g. an unknown member or trade id) to 400 instead of 500. */
@RestControllerAdvice
public class ApiErrors {

    private static final ResponseEntity<Map<String, String>> INVALID_REFERENCE =
            ResponseEntity.badRequest().body(Map.of("error", "Invalid reference: no matching record"));

    @ExceptionHandler(DataIntegrityViolationException.class)
    public ResponseEntity<Map<String, String>> constraintViolation(DataIntegrityViolationException e) {
        return INVALID_REFERENCE;
    }

    /** Spring has no SQLite error-code table, so SQLite constraint errors arrive uncategorized. */
    @ExceptionHandler(UncategorizedSQLException.class)
    public ResponseEntity<Map<String, String>> uncategorized(UncategorizedSQLException e) {
        if (e.getSQLException() instanceof SQLiteException se && se.getResultCode().name().startsWith("SQLITE_CONSTRAINT")) {
            return INVALID_REFERENCE;
        }
        throw e;
    }
}
