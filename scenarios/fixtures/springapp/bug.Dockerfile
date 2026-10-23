# The same service with variants/bug/BillingCalculator.java in place of the correct one:
# a defect in business code that no environment change can fix.
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY pom.xml .
COPY src src
COPY variants variants
RUN cp variants/bug/BillingCalculator.java src/main/java/demo/BillingCalculator.java
RUN --mount=type=cache,target=/root/.m2 mvn -q -B package -DskipTests

FROM eclipse-temurin:17-jre
COPY --from=build /src/target/app.jar /app.jar
EXPOSE 8080
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app.jar"]
