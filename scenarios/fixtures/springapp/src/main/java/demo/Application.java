package demo;

import org.springframework.boot.ApplicationRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

/** Runs one task at start (a failure stops the service) and again on GET /check. */
@SpringBootApplication
@RestController
public class Application {
    private final Tasks tasks = new Tasks(System.getenv());

    public static void main(String[] args) {
        SpringApplication.run(Application.class, args);
    }

    @Bean
    ApplicationRunner startup() {
        return args -> tasks.start();
    }

    @GetMapping("/health")
    String health() {
        return "ok";
    }

    @GetMapping("/check")
    ResponseEntity<String> check() {
        try {
            return ResponseEntity.ok(tasks.check());
        } catch (Exception e) {
            return ResponseEntity.status(500).body(e.toString());
        }
    }
}
