package com.tracker.api;

import java.util.List;
import java.util.Map;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.tracker.repo.MemberRepository;

@RestController
@RequestMapping("/api")
public class WatchlistController {

    record WatchRequest(String memberId, String reason) {}

    private final MemberRepository members;

    public WatchlistController(MemberRepository members) {
        this.members = members;
    }

    @GetMapping("/members")
    public List<Map<String, Object>> members() {
        return members.members();
    }

    @GetMapping("/watchlist")
    public List<Map<String, Object>> watchlist() {
        return members.watchlist();
    }

    @PostMapping("/watchlist")
    public ResponseEntity<Void> watch(@RequestBody WatchRequest req) {
        if (req.memberId() == null || req.memberId().isBlank()) {
            return ResponseEntity.badRequest().build();
        }
        members.watch(req.memberId(), req.reason());
        return ResponseEntity.noContent().build();
    }

    @DeleteMapping("/watchlist/{memberId}")
    public ResponseEntity<Void> unwatch(@PathVariable String memberId) {
        members.unwatch(memberId);
        return ResponseEntity.noContent().build();
    }
}
