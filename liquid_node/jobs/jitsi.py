from liquid_node import jobs

class Jitsi(jobs.Job):
    name = 'jitsi'
    template = jobs.TEMPLATES / f'{name}.nomad'
    app = 'jitsi'
