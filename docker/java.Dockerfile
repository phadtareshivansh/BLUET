# Java sandbox image for BLUET
# Based on maven:3.9-eclipse-temurin-8 with jqwik for property-based testing

ARG BASE_IMAGE=maven:3.9-eclipse-temurin-8
FROM $BASE_IMAGE

# Create non-root user for security
RUN groupadd -r bluet && useradd -r -g bluet bluet

WORKDIR /workdir
USER bluet

ENTRYPOINT ["mvn"]