# coding=utf-8
import tensorflow as tf
import numpy as np
import sqlite3
from haversine import haversine
import geohash
import json
import math
import random
import time
import os

feature_num = 10
h1_num = 10


class RankNet(object):
    def __init__(self):
        self._labels = []
        self._features = []
        self._is_ready = False
        self._database = ''
        self._conn = None

        # global parameters
        self._mean_category_number = {}
        self._category_coefficient = {}
        self._categories = {}
        self._all_points = []

    def _read_train_data(self, train_file):
        print('[RankNet] Pre-calculated train file found, reading from external file...')
        start_time = time.clock()

        with open(train_file, 'r') as f:
            self._mean_category_number = json.loads(f.readline())
            self._category_coefficient = json.loads(f.readline())
            self._labels = json.loads(f.readline())
            self._features = json.loads(f.readline())

        end_time = time.clock()
        print('[RankNet] Training data read in %f seconds.' % (end_time - start_time))

    def _write_train_data(self, train_file):
        with open(train_file, 'w') as f:
            f.write(json.dumps(self._mean_category_number) + '\n')
            f.write(json.dumps(self._category_coefficient) + '\n')
            f.write(json.dumps(self._labels) + '\n')
            f.write(json.dumps(self._features))
        print('[RankNet] Calculated training data stored into %s.' % train_file)

    def _neighbor_categories(self, point):
        # calculate sub-global parameters
        neighbor_categories = {}
        for category, _ in self._categories.items():
            neighbor_categories[category] = 0
        for neighbor in point['neighbors']:
            neighbor_categories[unicode(neighbor['category'])] += 1
        return neighbor_categories

    def _vectorize_point(self, point, training_category):
        neighbor_categories = self._neighbor_categories(point)

        x = []

        # density
        x.append(len(point['neighbors']))

        # neighbors entropy
        entropy = 0
        for (key, value) in neighbor_categories.items():
            entropy += float(value) / len(point['neighbors']) * -1 * math.log(float(value) / len(point['neighbors']), 10)

        x.append(entropy)

        # competitiveness
        competitiveness = 0
        if training_category in neighbor_categories:
            competitiveness = -1 * float(neighbor_categories[training_category]) / len(point['neighbors'])

        x.append(competitiveness)

        # quality by jensen
        jenson_quality = 0
        for category, _ in self._categories.items():
            jenson_quality += math.log(self._category_coefficient[category][training_category]) * (
            neighbor_categories[category] - self._mean_category_number[category][training_category])

        # area popularity
        popularity = 0
        for neighbor in point['neighbors']:
            popularity += int(neighbor['checkins'])

        x.append(popularity)

    def _get_neighboring_points(self, lng, lat, geo_hash, r):
        neighbors = []
        potential_neighbors = []

        for point in self._conn.execute('''SELECT lat,lng,category,checkins,id FROM \'Beijing-Checkins\' 
                                                             WHERE geohash LIKE \'%s%%\'''' % geo_hash[:6]):
            potential_neighbors.append(point)

        for neighbor in potential_neighbors:
            if haversine((float(neighbor[0]), float(neighbor[1])), (float(lat), float(lng))) * 1000 <= r:
                neighbors.append({
                    'id': unicode(neighbor[4])
                })

        return neighbors

    def _expand_neighbors(self, point):
        for neighbor in point['neighbors']:
            row = self._conn.execute('''SELECT checkins,category FROM \'Beijing-Checkins\' WHERE id=? LIMIT 1''',
                                     unicode(neighbor['id'])).fetchone()[0]
            neighbor['checkins'] = unicode(row[0])
            neighbor['category'] = unicode(row[1])

        return point

    def _release_neighbors(self, point):
        for neighbor in point['neighbors']:
            del neighbor['checkins']
            del neighbor['category']

    def _calculate_train_data(self):
        from progress.bar import Bar

        print('[RankNet] Pre-calculated train file not found, calculating training data...')
        start_time = time.clock()

        c = self._conn.cursor()
        # if geohash has never been calculated
        if c.execute('''SELECT geohash FROM \'Beijing-Checkins\' LIMIT 1''').fetchone()[0] is None:
            # calculate the geohash value and store in database
            for row in c.execute('''SELECT id,lng,lat FROM \'Beijing-Checkins\''''):
                self._conn.execute('''UPDATE \'Beijing-Checkins\' set geohash=? WHERE id=?''',
                             (geohash.encode(float(row[2]), float(row[1])), row[0]))
            self._conn.commit()

        # radius to calculate features
        r = 200

        # calculate global parameters
        total_num = int(self._conn.execute('''SELECT COUNT(*) FROM \'Beijing-Checkins\'''').fetchone()[0])
        categories = {}
        for row in self._conn.execute('''SELECT category, COUNT(*) AS num FROM "Beijing-Checkins" GROUP BY category'''):
            categories[unicode(row[0])] = int(row[1])

        # calculate and store the neighboring points
        bar = Bar('Calculating neighbors', suffix='%(index)d / %(max)d, %(percent)d%%', max=total_num)
        for row in self._conn.execute('''SELECT lng,lat,geohash,category,checkins,id FROM \'Beijing-Checkins\''''):
            self._all_points.append({
                'id': unicode(row[5]),
                'checkins': unicode(row[4]),
                'category': unicode(row[3]),
                'neighbors': self._get_neighboring_points(row[0], row[1], row[2], r)
            })
            bar.next()
        bar.finish()

        # calculate global category parameters
        for outer, _ in categories.items():
            self._mean_category_number[outer] = {}
            self._category_coefficient[outer] = {}
            for inner, _ in categories.items():
                self._mean_category_number[outer][inner] = 0
                self._category_coefficient[outer][inner] = 0

        # calculate mean category numbers
        bar = Bar('Calculating mean category numbers', suffix='%(index)d / %(max)d, %(percent)d%%', max=total_num)
        for point in self._all_points:
            self._expand_neighbors(point)
            for neighbor in point['neighbors']:
                self._mean_category_number[neighbor['category']][point['category']] += 1
            self._release_neighbors(point)

            bar.next()

        for p, _ in categories.items():
            for l, _ in categories.items():
                self._mean_category_number[p][l] /= categories[l]

        bar.finish()

        # calculate category coefficients
        bar = Bar('Calculating category coefficients', suffix='%(index)d / %(max)d, %(percent)d%%', max=len(categories) * len(categories))
        for p, _ in categories.items():
            for l, _ in categories.items():
                k_prefix = float(total_num - categories[p]) / (categories[p] * categories[l])

                k_suffix = 0
                for pt in self._all_points:
                    if pt['category'] == p:
                        neighbor_categories = self._neighbor_categories(pt)

                        k_suffix += float(neighbor_categories[l]) / (len(pt['neighbors']) - neighbor_categories[p])

                self._category_coefficient[p][l] = k_prefix * k_suffix

                bar.next()

        bar.finish()

        bar = Bar('Calculating features', suffix='%(index)d / %(max)d, %(percent)d%%', max=total_num)
        # calculate features
        for point in self._all_points:
            self._expand_neighbors(point)
            # add label
            self._labels.append([int(point['checkins'])])
            # add feature
            self._features.append(self._vectorize_point(point, u'生活娱乐'))
            self._release_neighbors(point)
            bar.next()

        bar.finish()

        end_time = time.clock()
        print('[RankNet] Training data calculated in %f seconds.' % (end_time - start_time))

        # store calculated train data
        self._write_train_data(os.path.dirname(self._database) + '/train.txt')

    def get_train_data(self, batch_size=32):
        # generate data with 10 dimensions
        X1, X2 = [], []
        Y1, Y2 = [], []

        for i in range(0, batch_size):
            x1 = []
            x2 = []
            o1 = 0.0
            o2 = 0.0
            for j in range(0, 10):
                r1 = random.random()
                r2 = random.random()
                x1.append(r1)
                x2.append(r2)

                mu = 2.0
                if j >= 5: mu = 3.0
                o1 += r1 * mu
                o2 += r2 * mu
            X1.append(x1)
            Y1.append([o1])
            X2.append(x2)
            Y2.append([o2])

        return (np.array(X1), np.array(X2)), (np.array(Y1), np.array(Y2))

    def train(self, database, train_file):
        # open database connection
        self._database = database
        self._conn = sqlite3.connect(database)

        if train_file is None or not os.path.exists(train_file):
            self._calculate_train_data()
        else:
            self._read_train_data(train_file)

        print('[TensorFlow] Start training model...')
        start_time = time.clock()

        with tf.name_scope("input"):
            x1 = tf.placeholder(tf.float32, [None, feature_num], name="x1")
            x2 = tf.placeholder(tf.float32, [None, feature_num], name="x2")

            o1 = tf.placeholder(tf.float32, [None, 1], name="o1")
            o2 = tf.placeholder(tf.float32, [None, 1], name="o2")

        # add layer1
        with tf.name_scope("layer1"):
            with tf.name_scope("w1"):
                w1 = tf.Variable(tf.random_normal([feature_num, h1_num]), name="w1")
                # tf.summary.histogram("layer1/w1", w1)
            with tf.name_scope("b1"):
                b1 = tf.Variable(tf.random_normal([h1_num]), name="b1")
                # tf.summary.histogram("layer1/b1", b1)

            # didn't add activation function
            with tf.name_scope("h1_o1"):
                h1_o1 = tf.matmul(x1, w1) + b1
                # tf.summary.histogram("h1_o1", h1_o1)

            with tf.name_scope("h2_o1"):
                h1_o2 = tf.matmul(x2, w1) + b1
                # tf.summary.histogram("h2_o1", h1_o2)

        # add output layer
        with tf.name_scope("output"):
            with tf.name_scope("w2"):
                w2 = tf.Variable(tf.random_normal([h1_num, 1]), name="w2")
                # tf.summary.histogram("output/w2", w2)

            with tf.name_scope("b2"):
                b2 = tf.Variable(tf.random_normal([1]))
                # tf.summary.histogram("output/b2", b2)

            h2_o1 = tf.matmul(h1_o1, w2) + b2
            h2_o2 = tf.matmul(h1_o2, w2) + b2

        # calculate probability based on output layer
        with tf.name_scope("loss"):
            o12 = o1 - o2
            h_o12 = h2_o1 - h2_o2

            pred = 1 / (1 + tf.exp(-h_o12))
            lable_p = 1 / (1 + tf.exp(-o12))

            cross_entropy = -lable_p * tf.log(pred) - (1 - lable_p) * tf.log(1 - pred)
            reduce_sum = tf.reduce_sum(cross_entropy, 1)
            loss = tf.reduce_mean(reduce_sum)
            tf.summary.scalar("loss", loss)

        with tf.name_scope("train_op"):
            train_op = tf.train.GradientDescentOptimizer(0.1).minimize(loss)

        with tf.Session() as sess:
            # summary_op = tf.summary.merge_all()
            # writer = tf.summary.FileWriter("./logs/", sess.graph)
            init = tf.global_variables_initializer()
            sess.run(init)
            for epoch in range(0, 100):
                X, Y = self.get_train_data()
                sess.run(train_op, feed_dict={x1: X[0], x2: X[1], o1: Y[0], o2: Y[1]})
                if epoch % 10 == 0:
                    # summary_result = sess.run(summary_op, feed_dict={x1: X[0], x2: X[1], o1: Y[0], o2: Y[1]})
                    # writer.add_summary(summary_result, epoch)
                    l_v = sess.run(loss, feed_dict={x1: X[0], x2: X[1], o1: Y[0], o2: Y[1]})
                    h_o12_v = sess.run(h_o12, feed_dict={x1: X[0], x2: X[1], o1: Y[0], o2: Y[1]})
                    o12_v = sess.run(o12, feed_dict={x1: X[0], x2: X[1], o1: Y[0], o2: Y[1]})
                    #print "------ epoch[%d] loss_v[%f] ------ " % (epoch, l_v)
                    #for k in range(0, len(o12_v)):
                        #print "k[%d] o12_v[%f] h_o12_v[%f]" % (k, o12_v[k], h_o12_v[k])

        end_time = time.clock()

        print '[TensorFlow] Model trained in %f seconds' % (end_time - start_time)
        return

    def rank(self, query_points, caller):
        print('[TensorFlow - 0x%x] Start ranking the query points with size %d.' % (id(caller), len(query_points)))
        for point in query_points:
            x = self._vectorize_point(point)
            # TODO: calculate the score with x
            score = 0
            point.append(score)
        print('[TensorFlow - 0x%x] Ranking finished.' % id(caller))

        return query_points
