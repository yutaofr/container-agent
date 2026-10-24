# Builds inside Docker, so the host needs no JDK. The Maven cache mount keeps dependency
# downloads between builds.
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /src
COPY pom.xml .
COPY src src
RUN --mount=type=cache,target=/root/.m2 mvn -q -B package -DskipTests

FROM eclipse-temurin:17-jre
COPY --from=build /src/target/app.jar /app.jar
EXPOSE 8080
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app.jar"]
