package demo;

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import org.apache.kafka.clients.admin.Admin;
import org.apache.kafka.clients.admin.NewTopic;
import org.apache.kafka.clients.consumer.ConsumerRecord;
import org.apache.kafka.clients.consumer.KafkaConsumer;
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.errors.TopicExistsException;
import org.apache.kafka.common.serialization.StringDeserializer;
import org.apache.kafka.common.serialization.StringSerializer;

/** The work each APP_TASK does. Every task uses its dependency for real. */
class Tasks {
    private static final int HEAP_MB = 150;
    private static final List<byte[]> held = new ArrayList<>();
    private final Map<String, String> env;

    Tasks(Map<String, String> env) {
        this.env = env;
    }

    void start() throws Exception {
        switch (env.getOrDefault("APP_TASK", "")) {
            case "heap" -> {
                for (int i = 0; i < HEAP_MB; i++) {
                    held.add(new byte[1 << 20]);
                }
            }
            case "orders" -> {
                try (Connection c = connect()) {
                    c.createStatement().execute(
                        "CREATE TABLE IF NOT EXISTS orders (id VARCHAR(64) PRIMARY KEY, total INT)");
                }
                check();
            }
            case "events" -> check();
            case "billing" -> {
                if (!"false".equals(env.get("BILLING_ENABLED"))) {
                    check();
                }
            }
            default -> throw new IllegalStateException("unknown APP_TASK " + env.get("APP_TASK"));
        }
    }

    String check() throws Exception {
        return switch (env.getOrDefault("APP_TASK", "")) {
            case "heap" -> "holding " + held.size() + " MB";
            case "orders" -> order();
            case "events" -> roundTrip();
            case "billing" -> "invoice total " + BillingCalculator.total(List.of(
                new BillingCalculator.Line(2, 1500, false),
                new BillingCalculator.Line(1, 4000, false)));
            default -> throw new IllegalStateException("unknown APP_TASK");
        };
    }

    private Connection connect() throws Exception {
        return DriverManager.getConnection(env.get("SPRING_DATASOURCE_URL"),
            env.get("SPRING_DATASOURCE_USERNAME"), env.get("SPRING_DATASOURCE_PASSWORD"));
    }

    private String order() throws Exception {
        String id = UUID.randomUUID().toString();
        try (Connection c = connect()) {
            try (PreparedStatement s = c.prepareStatement("INSERT INTO orders VALUES (?, ?)")) {
                s.setString(1, id);
                s.setInt(2, 42);
                s.executeUpdate();
            }
            try (PreparedStatement s = c.prepareStatement("SELECT total FROM orders WHERE id = ?")) {
                s.setString(1, id);
                ResultSet r = s.executeQuery();
                if (!r.next() || r.getInt(1) != 42) {
                    throw new IllegalStateException("order " + id + " was not stored");
                }
            }
        }
        return "stored order " + id;
    }

    /**
     * Creates the topic and waits for its leader, so clients never log the transient
     * metadata errors that auto-creation causes (a healthy service then logs no error).
     */
    private void ensureTopic(String servers) throws Exception {
        Properties a = new Properties();
        a.put("bootstrap.servers", servers);
        a.put("request.timeout.ms", "10000");
        a.put("default.api.timeout.ms", "20000");
        try (Admin admin = Admin.create(a)) {
            if (!admin.listTopics().names().get().contains("events")) {
                try {
                    admin.createTopics(List.of(new NewTopic("events", 1, (short) 1))).all().get();
                } catch (ExecutionException e) {
                    if (!(e.getCause() instanceof TopicExistsException)) {
                        throw e;
                    }
                }
            }
            long deadline = System.currentTimeMillis() + 20000;
            while (admin.describeTopics(List.of("events")).allTopicNames().get().get("events")
                    .partitions().get(0).leader() == null) {
                if (System.currentTimeMillis() > deadline) {
                    throw new IllegalStateException("topic events has no leader after 20 s");
                }
                Thread.sleep(200);
            }
        }
    }

    private String roundTrip() throws Exception {
        String servers = env.get("SPRING_KAFKA_BOOTSTRAP_SERVERS");
        ensureTopic(servers);
        String id = UUID.randomUUID().toString();
        Properties p = new Properties();
        p.put("bootstrap.servers", servers);
        p.put("key.serializer", StringSerializer.class.getName());
        p.put("value.serializer", StringSerializer.class.getName());
        p.put("max.block.ms", "20000");
        p.put("request.timeout.ms", "10000");
        p.put("delivery.timeout.ms", "20000");
        try (KafkaProducer<String, String> producer = new KafkaProducer<>(p)) {
            producer.send(new ProducerRecord<>("events", id, id)).get();
        }
        Properties c = new Properties();
        c.put("bootstrap.servers", servers);
        c.put("enable.auto.commit", "false");
        c.put("key.deserializer", StringDeserializer.class.getName());
        c.put("value.deserializer", StringDeserializer.class.getName());
        // assign, not subscribe: no consumer group, so no group-join chatter in the log
        try (KafkaConsumer<String, String> consumer = new KafkaConsumer<>(c)) {
            TopicPartition partition = new TopicPartition("events", 0);
            consumer.assign(List.of(partition));
            consumer.seekToBeginning(List.of(partition));
            long deadline = System.currentTimeMillis() + 30000;
            while (System.currentTimeMillis() < deadline) {
                for (ConsumerRecord<String, String> r : consumer.poll(Duration.ofSeconds(1))) {
                    if (id.equals(r.value())) {
                        return "event " + id + " produced and consumed";
                    }
                }
            }
        }
        throw new IllegalStateException("event " + id + " was not consumed within 30 s");
    }
}
