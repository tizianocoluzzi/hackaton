"""Connessione AMQP comune ai due servizi Python."""
import os
import pika


def connect():
    return pika.BlockingConnection(pika.ConnectionParameters(
        host=os.getenv('AMQP_HOST', 'rabbitmq'),
        port=int(os.getenv('AMQP_PORT', '5672')),
        virtual_host=os.getenv('AMQP_VHOST', '/'),
        credentials=pika.PlainCredentials(os.environ['AMQP_USER'], os.environ['AMQP_PASSWORD']),
        heartbeat=60, blocked_connection_timeout=30, socket_timeout=10,
        connection_attempts=1))


def properties():
    return pika.BasicProperties(content_type='application/json', delivery_mode=2)
