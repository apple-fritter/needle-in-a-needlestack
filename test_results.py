import concurrent
import copy
import json
import math
import os
import threading
from datetime import datetime

import matplotlib.pyplot as plt
from matplotlib import colors
import statsmodels.api as sm

from base_test_results import BaseTestResults, BaseStatusReport
from evaluator import DefaultEvaluator
from limerick import Limerick
from llm_client import PROMPT_RETRIES, backoff_after_exception
from prompt import get_prompt

STATUS_REPORT_INTERVAL = 5  # seconds
PLOT_FONT_SIZE = 8
NO_GENERATED_ANSWER = "ACEGIKMOQSUWY"


class QuestionAnswerCollector:
    def add_answer(self, question_id, answer, passed):
        raise NotImplementedError


class TestResultExceptionReport:
    def __init__(self, location_name, question_id, trial_number, attempt, exception_message):
        self.location_name = location_name
        self.question_id = question_id
        self.trial_number = trial_number
        self.attempt = attempt
        self.exception_message = exception_message

    def to_dict(self):
        result = copy.copy(vars(self))
        return result

    @staticmethod
    def from_dict(dictionary):
        result = TestResultExceptionReport(**dictionary)
        return result


class EvaluationExceptionReport:
    def __init__(self, location_name, question_id, trial_number, attempt, evaluation_model_name, exception_message):
        self.location_name = location_name
        self.question_id = question_id
        self.trial_number = trial_number
        self.attempt = attempt
        self.evaluation_model_name = evaluation_model_name
        self.exception_message = exception_message

    def to_dict(self):
        result = copy.copy(vars(self))
        return result

    @staticmethod
    def from_dict(dictionary):
        result = EvaluationExceptionReport(**dictionary)
        return result


class StatusReport(BaseStatusReport):
    def __init__(self, model_name, failed_test_count=0, failed_evaluation_count=0):
        super().__init__(failed_test_count, failed_evaluation_count)
        self.model_name = model_name

    def print(self):
        print("Model: ", self.model_name)
        print("Tests: ", self.test_count, " Answered: ", self.answered_test_count, " Finished: ",
              self.finished_test_count)
        print("Evaluators: ", self.evaluator_count, " Finished: ", self.finished_evaluator_count)
        for evaluator_model_name in self.waiting_for_evaluator_count:
            count = self.waiting_for_evaluator_count[evaluator_model_name]
            print("Waiting for evaluator: ", evaluator_model_name, " Count: ", count)
        print("Failed Tests: ", self.failed_test_count, " Failed Evaluations: ", self.failed_evaluation_count)
        print("--------------------")


class ScoreAccumulator:
    def __init__(self):
        self.cumulative_score = 0
        self.count = 0

    def add_score(self, score):
        self.cumulative_score += score
        self.count += 1

    def get_score(self):
        if self.count > 0:
            result = self.cumulative_score / self.count
        else:
            result = 0
        return result


class TrialScore:
    def __init__(self, trial_number, score):
        self.trial_number = trial_number
        self.score = score

    def to_dict(self):
        result = copy.copy(vars(self))
        return result

    @staticmethod
    def from_dict(dictionary):
        result = TrialScore(**dictionary)
        return result


class QuestionPlotLine:
    def __init__(self, question):
        self.question = question
        self.scores = []

    def add_score(self, score):
        self.scores.append(score)


class QuestionScore:
    def __init__(self, question, score):
        self.question = question
        self.score = score

    def to_dict(self):
        result = copy.copy(vars(self))
        result["question"] = self.question.to_dict()
        return result

    @staticmethod
    def from_dict(dictionary):
        question = dictionary.get("question", None)
        if question is not None:
            dictionary.pop("question", None)
            question = Limerick.from_dict(question)
            dictionary["question"] = question
        result = QuestionScore(**dictionary)
        return result


class LocationScore:
    def __init__(self, location_token_position, score, trial_scores=None, question_scores=None):
        self.location_token_position = location_token_position
        self.score = score
        self.trial_scores = trial_scores
        self.question_scores = question_scores

    def get_trial_score(self, trial_number):
        result = None
        for trial_score in self.trial_scores:
            if trial_score.trial_number == trial_number:
                result = trial_score
                break
        return result

    def get_question_score(self, question_id):
        result = None
        for question_score in self.question_scores:
            if question_score.question.id == question_id:
                result = question_score
                break
        return result

    def to_dict(self):
        result = copy.copy(vars(self))
        if self.trial_scores is not None:
            index = 0
            for trial_score in self.trial_scores:
                result["trial_scores"][index] = trial_score.to_dict()
                index += 1
        if self.question_scores is not None:
            index = 0
            for question_score in self.question_scores:
                result["question_scores"][index] = question_score.to_dict()
                index += 1
        return result

    @staticmethod
    def from_dict(dictionary):
        trial_scores = dictionary.get("trial_scores", None)
        if trial_scores is not None:
            dictionary.pop("trial_scores", None)
            trial_scores = [TrialScore.from_dict(trial) for trial in trial_scores]
            dictionary["trial_scores"] = trial_scores
        question_scores = dictionary.get("question_scores", None)
        if question_scores is not None:
            dictionary.pop("question_scores", None)
            question_scores = [QuestionScore.from_dict(question) for question in question_scores]
            dictionary["question_scores"] = question_scores
        result = LocationScore(**dictionary)
        return result


class ModelScore:
    def __init__(self, model_name, date_string="No date", repeat_question_limerick_count=1,
                 limerick_count_in_prompt=300,
                 location_scores=None, number_of_trials_per_location=None):
        self.model_name = model_name
        self.date_string = date_string
        self.repeat_question_limerick_count = repeat_question_limerick_count
        self.limerick_count_in_prompt = limerick_count_in_prompt
        self.location_scores = location_scores
        self.number_of_trials_per_location = number_of_trials_per_location

    def to_dict(self):
        result = copy.copy(vars(self))
        if self.location_scores is not None:
            index = 0
            for location_score in self.location_scores:
                result["location_scores"][index] = location_score.to_dict()
                index += 1
        return result

    def get_location_trial_scores(self):
        trial_name_list = [trial_score.trial_number for trial_score in self.location_scores[0].trial_scores]
        result = []
        for trial_name in trial_name_list:
            location_trial_scores = []
            for location_score in self.location_scores:
                trial_score = location_score.get_trial_score(trial_name)
                location_trial_scores.append(trial_score.score * 100)
            result.append(location_trial_scores)
        return result

    def get_location_question_scores(self):
        question_plot_list = [QuestionPlotLine(question_score.question) for question_score in self.location_scores[0].question_scores]
        for question_plot in question_plot_list:
            for location_score in self.location_scores:
                question_score = location_score.get_question_score(question_plot.question.id)
                question_plot.add_score(question_score.score * 100)
        return question_plot_list

    def write_trial_plot(self, plot_file_name):
        location_trial_score_list = self.get_location_trial_scores()
        self.write_plot(plot_file_name, location_trial_score_list)

    def write_question_plot(self, base_plot_file_name):
        if self.location_scores[0].question_scores is None:
            print("No question scores")
            return
        location_question_score_list = self.get_location_question_scores()
        for question_plot in location_question_score_list:
            plot_file_name = base_plot_file_name + str(question_plot.question.id) + ".png"
            self.write_line_plot(plot_file_name, question_plot.question.text, question_plot.question.question,
                                    question_plot.scores)

    def write_line_plot(self, plot_file_name, limerick, question_text, question_score_list):
        figure, axes = plt.subplots(figsize=(6, 4))
        labels = [location.location_token_position for location in self.location_scores]
        average_values = [round(location.score * 100) for location in self.location_scores]
        number_of_locations = len(average_values)
        number_of_trials = self.number_of_trials_per_location
        percent = round((self.repeat_question_limerick_count / self.limerick_count_in_prompt) * 100, 1)
        plot_title = f'{self.model_name} on {self.date_string}\nAsk question about {percent}% of {self.limerick_count_in_prompt} limericks in {number_of_trials} trials at {number_of_locations} token positions'
        axes.plot(labels, question_score_list, linewidth=2, color='#b3d0fc', label="Question", marker='.')
        axes.plot(labels, average_values, linewidth=1, color='darkblue', label="Average", marker='.')

        axes.set_title(plot_title, fontsize=PLOT_FONT_SIZE)
        axes.set_xticks(labels)
        x_labels = self.generate_x_labels(labels)
        axes.set_xticklabels(x_labels)
        axes.set_xlabel('Token Position', fontsize=PLOT_FONT_SIZE)
        axes.set_yticks(range(0, 100 + 1, 20))
        axes.set_yticklabels([f'{p}%' for p in range(0, 100 + 1, 20)])
        axes.set_ylabel('Percent Correct', fontsize=PLOT_FONT_SIZE)
        axes.spines['top'].set_visible(False)  # turns off the top "spine" completely
        axes.spines['right'].set_visible(False)
        axes.spines['left'].set_linewidth(.5)
        axes.spines['bottom'].set_linewidth(.5)
        axes.grid(False)
        axes.set_ylim(-3, 103)
        plt.legend()
        plt.subplots_adjust(bottom=0.4)
        plt.figtext(0.5, 0.04, f"{limerick}\n{question_text}", ha="center", fontsize=8,
                    bbox={"facecolor": "lightgrey", "alpha": 0.5, "pad": 5})

        #plt.tight_layout()
        plt.savefig(plot_file_name, dpi=300)
        plt.close()

    def write_plot(self, plot_file_name, subplot_data_list):
        figure, axes = plt.subplots(figsize=(7, 5))
        labels = [location.location_token_position for location in self.location_scores]
        values = [round(location.score * 100) for location in self.location_scores]
        number_of_locations = len(values)
        number_of_trials = self.number_of_trials_per_location
        percent = round((self.repeat_question_limerick_count / self.limerick_count_in_prompt) * 100, 1)
        plot_title = f'Tested {self.model_name} on {self.date_string}\nAsk question about {percent}% of {self.limerick_count_in_prompt} limericks in {number_of_trials} trials at {number_of_locations} token positions'
        for subplot_data in subplot_data_list:
            axes.scatter(labels, subplot_data, linewidth=.3, edgecolor='grey', color="#b3d0fc",
                         s=20, alpha=0.3)
        axes.plot(labels, values, linewidth=1, color='darkblue', label="Average", marker='.')

        axes.set_title(plot_title, fontsize=PLOT_FONT_SIZE)
        axes.set_xticks(labels)
        x_labels = self.generate_x_labels(labels)
        axes.set_xticklabels(x_labels)
        axes.set_xlabel('Token Position', fontsize=PLOT_FONT_SIZE)
        axes.set_yticks(range(0, 100 + 1, 20))
        axes.set_yticklabels([f'{p}%' for p in range(0, 100 + 1, 20)])
        axes.set_ylabel('Percent Correct', fontsize=PLOT_FONT_SIZE)
        axes.spines['top'].set_visible(False)  # turns off the top "spine" completely
        axes.spines['right'].set_visible(False)
        axes.spines['left'].set_linewidth(.5)
        axes.spines['bottom'].set_linewidth(.5)
        axes.grid(False)
        axes.set_ylim(-3, 103)
        # plt.legend()
        plt.tight_layout()
        plt.savefig(plot_file_name, dpi=300)
        plt.close()

    def generate_x_labels(self, labels):
        result = []
        labels_length = len(labels)
        end = labels_length - 1
        if len(labels) <= 5:
            result = [f'{round(label / 1000, 1)}k' for label in labels]
        elif len(labels) == 10:
            for index, label in enumerate(labels):
                if index in (0, 3, 6, 9):
                    result.append(f'{round(label / 1000, 1)}k')
                else:
                    result.append('')
        elif len(labels) == 20:
            for index, label in enumerate(labels):
                if index in (0, 6, 13, 19):
                    result.append(f'{round(label / 1000, 1)}k')
                else:
                    result.append('')
        elif len(labels) % 2 != 0:  # odd number of labels
            for index, label in enumerate(labels):
                if index in (0, labels_length / 2, end):
                    result.append(f'{round(label / 1000, 1)}k')
                else:
                    result.append('')
        else:
            gap = math.floor(labels_length / 3)
            for index, label in enumerate(labels):
                if index in (0, gap, end - gap, end):
                    result.append(f'{round(label / 1000, 1)}k')
                else:
                    result.append('')
        return result

    @staticmethod
    def from_dict(dictionary):
        location_scores = dictionary.get("location_scores", None)
        if location_scores is not None:
            dictionary.pop("location_scores", None)
            location_scores = [LocationScore.from_dict(location) for location in location_scores]
            dictionary["location_scores"] = location_scores
        result = ModelScore(**dictionary)
        return result


class TestModelScores:
    def __init__(self, model_scores=None):
        self.model_scores = model_scores

    def to_dict(self):
        result = copy.copy(vars(self))
        if self.model_scores is not None:
            index = 0
            for model_score in self.model_scores:
                result["model_scores"][index] = model_score.to_dict()
                index += 1
        return result

    def write_to_file(self, file_path):
        with open(file_path, "w") as file:
            results_dict = self.to_dict()
            json.dump(results_dict, file, indent=4)

    @staticmethod
    def from_dict(dictionary):
        model_scores = dictionary.get("model_scores", None)
        if model_scores is not None:
            dictionary.pop("model_scores", None)
            model_scores = [ModelScore.from_dict(result) for result in model_scores]
            dictionary["model_scores"] = model_scores
        result = TestModelScores(**dictionary)
        return result


class EvaluatorResult:
    def __init__(self, model_name, passed=None):
        self.model_name = model_name
        self.passed = passed

    def set_passed(self, passed):
        self.passed = passed

    def is_finished(self):
        return self.passed is not None

    def to_dict(self):
        result = copy.copy(vars(self))
        return result

    @staticmethod
    def from_dict(dictionary):
        result = EvaluatorResult(**dictionary)
        return result


class TrialResult:
    def __init__(self, trial_number, good_answer, passed=None, dissent_count=None,
                 generated_answer=None, evaluator_results=None):
        self.trial_number = trial_number
        self.good_answer = good_answer
        self.passed = passed
        self.dissent_count = dissent_count
        self.generated_answer = generated_answer
        self.evaluator_results = evaluator_results
        if evaluator_results is None:
            self.evaluator_results = []

    def add_evaluator(self, model_name):
        evaluator_result = EvaluatorResult(model_name)
        self.evaluator_results.append(evaluator_result)

    def run_trial(self, results, prompt_text, model, evaluator, location, question):
        for attempt in range(PROMPT_RETRIES):
            try:
                tmp_generated_answer = model.prompt(prompt_text)
                break
            except Exception as e:
                tmp_generated_answer = NO_GENERATED_ANSWER
                results.add_test_exception(model.llm_name, location, question.id, self.trial_number, attempt, e)
                if attempt == 2:
                    print("Exception on attempt 3")
                backoff_after_exception(attempt)
                continue
        results.set_test_result(model.llm_name, location, question.id, self.trial_number, tmp_generated_answer)
        score = evaluator.evaluate(model.llm_name, location, question, self.trial_number, tmp_generated_answer)
        return tmp_generated_answer, score

    def is_finished(self):
        return self.passed is not None

    def has_answer(self):
        return self.generated_answer is not None

    def has_dissent(self):
        return self.dissent_count > 0

    def has_concerning_dissent(self):
        concerning_dissent_count = round(len(self.evaluator_results) / 2)
        result = self.dissent_count >= concerning_dissent_count
        return result

    def calculate_scores(self, status_report):
        finished = True
        passed_count = 0
        for evaluator_result in self.evaluator_results:
            status_report.add_evaluator_test(evaluator_result.is_finished(), evaluator_result.model_name)
            if evaluator_result.is_finished():
                if evaluator_result.passed:
                    passed_count += 1
            else:
                finished = False
        if finished:
            self.passed = passed_count > (len(self.evaluator_results) / 2)
            self.dissent_count = 0
            for evaluator_result in self.evaluator_results:
                if evaluator_result.passed != self.passed:
                    self.dissent_count += 1
        status_report.add_test(self.has_answer(), self.is_finished())

    def set_generated_answer(self, generated_answer):
        self.generated_answer = generated_answer

    def set_evaluator_result(self, evaluator_model_name, passed):
        for evaluator_result in self.evaluator_results:
            if evaluator_result.model_name == evaluator_model_name and not evaluator_result.is_finished():
                evaluator_result.set_passed(passed)
                break

    def to_dict(self):
        result = copy.copy(vars(self))
        if self.evaluator_results is not None:
            index = 0
            for evaluator_result in self.evaluator_results:
                result["evaluator_results"][index] = evaluator_result.to_dict()
                index += 1
        return result

    @staticmethod
    def from_dict(dictionary):
        evaluator_results = dictionary.get("evaluator_results", None)
        if evaluator_results is not None:
            dictionary.pop("evaluator_results", None)
            evaluator_results = [EvaluatorResult.from_dict(result) for result in evaluator_results]
            dictionary["evaluator_results"] = evaluator_results
        result = TrialResult(**dictionary)
        return result

    @staticmethod
    def create(trial_number, good_answer, evaluator_model_list):
        result = TrialResult(trial_number, good_answer)
        for model in evaluator_model_list:
            result.add_evaluator(model.llm_name)
        return result


class QuestionResults:
    def __init__(self, question, trial_results=None, score=None):
        self.question = question
        self.trial_results = trial_results if trial_results is not None else []
        self.score = score

    def add_trial(self, trial_number, evaluator_model_list):
        trial_result = TrialResult.create(trial_number, self.question.answer, evaluator_model_list)
        self.trial_results.append(trial_result)

    def run_trials(self, thread_pool, results, prompt_text, model, evaluator, location):
        futures_list = []
        for trial_result in self.trial_results:
            futures_list.append(thread_pool.submit(trial_result.run_trial, results, prompt_text, model, evaluator,
                                                   location, self.question))
        return futures_list

    def get_trial(self, trial_number):
        result = self.trial_results[trial_number]
        return result

    def get_trial_names(self):
        result = [trial_result.trial_number for trial_result in self.trial_results]
        return result

    def add_score_for_trial(self, trial_name, score_accumulator):
        for trial_result in self.trial_results:
            if trial_result.trial_number == trial_name:
                score_accumulator.add_score(trial_result.passed)
                break

    def calculate_scores(self, status_report):
        correct_results = 0
        finished_trials = 0
        for trial_result in self.trial_results:
            trial_result.calculate_scores(status_report)
            if trial_result.is_finished():
                finished_trials += 1
                if trial_result.passed:
                    correct_results += 1
        if finished_trials > 0:
            self.score = correct_results / finished_trials
        return self.score

    def to_dict(self):
        result = copy.copy(vars(self))
        result["question"] = self.question.to_dict()
        if self.trial_results is not None:
            index = 0
            for trial_result in self.trial_results:
                result["trial_results"][index] = trial_result.to_dict()
                index += 1
        return result

    @staticmethod
    def from_dict(dictionary):
        question = dictionary.get("question", None)
        if question is not None:
            dictionary.pop("question", None)
            question = Limerick.from_dict(question)
            dictionary["question"] = question
        trial_results = dictionary.get("trial_results", None)
        if trial_results is not None:
            dictionary.pop("trial_results", None)
            trial_results = [TrialResult.from_dict(result) for result in trial_results]
            dictionary["trial_results"] = trial_results
        result = QuestionResults(**dictionary)
        return result

    @staticmethod
    def create(question, trial_count, evaluator_model_list):
        result = QuestionResults(question)
        for trial_number in range(trial_count):
            result.add_trial(trial_number, evaluator_model_list)
        return result


class LocationResults:
    def __init__(self, location_token_position, question_result_list=None, score=None):
        self.location_token_position = location_token_position
        self.question_result_list = question_result_list if question_result_list is not None else []
        self.score = score

    def add_question(self, question, trial_count, evaluator_model_list):
        question_result = QuestionResults.create(question, trial_count, evaluator_model_list)
        self.question_result_list.append(question_result)

    def run_trials(self, thread_pool, results, model, evaluator):
        futures_list = []
        for question_result in self.question_result_list:
            prompt_text = results.get_prompt(model.llm_name, self.location_token_position, question_result.question.id)
            futures_list += question_result.run_trials(thread_pool, results, prompt_text, model, evaluator,
                                                       self.location_token_position)
        return futures_list

    def get_trial(self, question_id, trial_number):
        result = None
        for question_result in self.question_result_list:
            if question_result.question.id == question_id:
                result = question_result.get_trial(trial_number)
                break
        return result

    def calculate_scores(self, status_report):
        cumulative_score = 0.0
        score_count = 0
        for question_result in self.question_result_list:
            question_score = question_result.calculate_scores(status_report)
            if question_score is not None:
                cumulative_score += question_score
                score_count += 1
        if score_count > 0:
            self.score = cumulative_score / score_count
        return self.score

    def get_trial_scores(self):
        result = []
        trial_name_list = self.question_result_list[0].get_trial_names()
        for trial_name in trial_name_list:
            accumulated_score = ScoreAccumulator()
            for question_result in self.question_result_list:
                question_result.add_score_for_trial(trial_name, accumulated_score)
            trial_score = TrialScore(trial_name, accumulated_score.get_score())
            result.append(trial_score)
        return result

    def get_question_scores(self):
        result = []
        for question_result in self.question_result_list:
            question_score = QuestionScore(question_result.question, question_result.score)
            result.append(question_score)
        return result

    def to_dict(self):
        result = copy.copy(vars(self))
        if self.question_result_list is not None:
            index = 0
            for question_result in self.question_result_list:
                result["question_result_list"][index] = question_result.to_dict()
                index += 1
        return result

    @staticmethod
    def from_dict(dictionary):
        question_result_list = dictionary.get("question_result_list", None)
        if question_result_list is not None:
            dictionary.pop("question_result_list", None)
            question_result_list = [QuestionResults.from_dict(result) for result in question_result_list]
            dictionary["question_result_list"] = question_result_list
        result = LocationResults(**dictionary)
        return result

    @staticmethod
    def create(location_token_position, question_list, trials, evaluator_model_list):
        result = LocationResults(location_token_position)
        for question in question_list:
            result.add_question(question, trials, evaluator_model_list)
        return result


class ModelResults:
    def __init__(self, date_string, directory, model_name, number_of_trials_per_location,
                 repeat_question_limerick_count, location_list=None,
                 test_exception_list=None, evaluation_exception_list=None, failed_test_count=0,
                 failed_evaluation_count=0, limerick_count_in_prompt=None):
        self.date_string = date_string
        self.directory = directory
        self.model_name = model_name
        self.location_list = location_list
        self.number_of_trials_per_location = number_of_trials_per_location
        self.repeat_question_limerick_count = repeat_question_limerick_count
        self.limerick_count_in_prompt = limerick_count_in_prompt
        self.test_exception_list = test_exception_list
        if self.test_exception_list is None:
            self.test_exception_list = []
        self.failed_test_count = failed_test_count
        self.evaluation_exception_list = evaluation_exception_list
        if self.evaluation_exception_list is None:
            self.evaluation_exception_list = []
        self.failed_evaluation_count = failed_evaluation_count
        self.location_list = location_list
        if self.location_list is None:
            self.location_list = []
        if directory is not None and len(directory) > 0:
            os.makedirs(directory, exist_ok=True)

    def set_limerick_count_in_prompt(self, limerick_count_in_prompt):
        self.limerick_count_in_prompt = limerick_count_in_prompt

    def add_location(self, location_token_position, question_list, trials, evaluator_model_list):
        location = LocationResults.create(location_token_position, question_list, trials, evaluator_model_list)
        self.location_list.append(location)

    def run_trials(self, thread_pool, results, model, evaluator):
        futures_list = []
        for location in self.location_list:
            futures_list += location.run_trials(thread_pool, results, model, evaluator)
        return futures_list

    def get_trial(self, location_name, question_id, trial_number):
        result = None
        for location in self.location_list:
            if location.location_token_position == location_name:
                result = location.get_trial(question_id, trial_number)
                break
        return result

    def calculate_scores(self):
        status_report = StatusReport(self.model_name, self.failed_test_count, self.failed_evaluation_count)
        for location in self.location_list:
            location.calculate_scores(status_report)
        status_report.print()
        return status_report

    def get_location_scores(self):
        result = []
        for location in self.location_list:
            trial_scores = location.get_trial_scores()
            question_scores = location.get_question_scores()
            location_score = LocationScore(location.location_token_position, location.score, trial_scores,
                                           question_scores)
            result.append(location_score)
        return result

    def add_test_exception(self, location_name, question_id, trial_number, attempt, exception):
        print("Test Exception: ", str(exception))
        exception_report = TestResultExceptionReport(location_name, question_id, trial_number, attempt, str(exception))
        self.test_exception_list.append(exception_report)
        if attempt == PROMPT_RETRIES - 1:
            self.failed_test_count += 1
            print("Failed Test Count: ", self.failed_test_count)

    def add_evaluation_exception(self, location_name, question_id, trial_number, attempt, evaluation_model_name,
                                 exception):
        print("Evaluation Exception: ", str(exception))
        exception_report = EvaluationExceptionReport(location_name, question_id, trial_number, attempt,
                                                     evaluation_model_name, str(exception))
        self.evaluation_exception_list.append(exception_report)
        if attempt == PROMPT_RETRIES - 1:
            self.failed_evaluation_count += 1
            print("Failed Evaluation Count: ", self.failed_evaluation_count)

    def get_all_trial_results(self):
        result = []
        for location in self.location_list:
            for question_result in location.question_result_list:
                for trial_result in question_result.trial_results:
                    result.append(trial_result)
        return result

    def collect_question_answers(self, question_answer_collector: QuestionAnswerCollector):
        for location in self.location_list:
            for question_result in location.question_result_list:
                for trial_result in question_result.trial_results:
                    if trial_result.has_answer():
                        question_answer_collector.add_answer(question_result.question.id, trial_result.generated_answer,
                                                             trial_result.passed)

    def to_dict(self):
        result = copy.deepcopy(vars(self))
        if self.location_list is not None:
            index = 0
            for location in self.location_list:
                result["location_list"][index] = location.to_dict()
                index += 1
        if self.test_exception_list is not None:
            index = 0
            for exception_report in self.test_exception_list:
                result["test_exception_list"][index] = exception_report.to_dict()
                index += 1
        if self.evaluation_exception_list is not None:
            index = 0
            for exception_report in self.evaluation_exception_list:
                result["evaluation_exception_list"][index] = exception_report.to_dict()
                index += 1
        result.pop("directory", None)
        return result

    @staticmethod
    def from_dict(dictionary):
        location_list = dictionary.get("location_list", None)
        if location_list is not None:
            dictionary.pop("location_list", None)
            location_list = [LocationResults.from_dict(location) for location in location_list]
            dictionary["location_list"] = location_list
        test_exception_list = dictionary.get("test_exception_list", None)
        if test_exception_list is not None:
            dictionary.pop("test_exception_list", None)
            test_exception_list = [TestResultExceptionReport.from_dict(report) for report in test_exception_list]
            dictionary["test_exception_list"] = test_exception_list
        evaluation_exception_list = dictionary.get("evaluation_exception_list", None)
        if evaluation_exception_list is not None:
            dictionary.pop("evaluation_exception_list", None)
            evaluation_exception_list = [EvaluationExceptionReport.from_dict(report) for report in
                                         evaluation_exception_list]
            dictionary["evaluation_exception_list"] = evaluation_exception_list
        if "directory" not in dictionary:
            dictionary["directory"] = ""
        result = ModelResults(**dictionary)
        return result

    @staticmethod
    def create(date_string, directory, model_name, location_token_index_list, question_list,
               repeat_question_limerick_count,
               trial_count, evaluator_model_list):
        number_of_trials_per_location = trial_count * len(question_list)
        result = ModelResults(date_string, directory, model_name, number_of_trials_per_location,
                              repeat_question_limerick_count)
        for location_token_index in location_token_index_list:
            result.add_location(location_token_index, question_list, trial_count, evaluator_model_list)
        return result

    @staticmethod
    def from_file(file_path):
        result = None
        with open(file_path, "r") as file:
            results_dict = json.load(file)
            result = ModelResults.from_dict(results_dict)
        return result


class BaseTestResultAction:
    def __init__(self, results, model_name, location_name, question_id, trial_number):
        self.results = results
        self.model_name = model_name
        self.location_name = location_name
        self.question_id = question_id
        self.trial_number = trial_number

    def get_model_results(self):
        result = self.results.get_model_results(self.model_name)
        return result

    def get_trial(self):
        result = self.get_model_results().get_trial(self.location_name, self.question_id, self.trial_number)
        return result

    def execute(self):
        raise NotImplementedError


class SetTestResultAction(BaseTestResultAction):
    def __init__(self, results, model_name, location_name, question_id, trial_number, generated_answer):
        super().__init__(results, model_name, location_name, question_id, trial_number)
        self.generated_answer = generated_answer

    def execute(self):
        self.get_trial().set_generated_answer(self.generated_answer)


class SetEvaluatorResultAction(BaseTestResultAction):
    def __init__(self, results, model_name, location_name, question_id, trial_number, evaluator_model_name, passed):
        super().__init__(results, model_name, location_name, question_id, trial_number)
        self.evaluator_model_name = evaluator_model_name
        self.passed = passed

    def execute(self):
        self.get_trial().set_evaluator_result(self.evaluator_model_name, self.passed)


class AddTestExceptionAction(BaseTestResultAction):
    def __init__(self, results, model_name, location_name, question_id, trial_number, attempt, exception):
        super().__init__(results, model_name, location_name, question_id, trial_number)
        self.attempt = attempt
        self.exception = exception

    def execute(self):
        self.get_model_results().add_test_exception(self.location_name, self.question_id, self.trial_number,
                                                    self.attempt, self.exception)


class AddEvaluationExceptionAction(BaseTestResultAction):
    def __init__(self, results, model_name, location_name, question_id, trial_number, evaluation_model_name, attempt,
                 exception):
        super().__init__(results, model_name, location_name, question_id, trial_number)
        self.evaluation_model_name = evaluation_model_name
        self.attempt = attempt
        self.exception = exception

    def execute(self):
        self.get_model_results().add_evaluation_exception(self.location_name, self.question_id, self.trial_number,
                                                          self.evaluation_model_name, self.attempt, self.exception)


class TestResults(BaseTestResults):
    def __init__(self, config):
        self.config = config
        self.date_string = None
        self.test_result_directory = None
        self.prompt = None
        self.evaluator = DefaultEvaluator(self, self.config.evaluator_model_list)
        self.model_results_list = []
        self.action_list = []
        self.action_list_lock = threading.Lock()
        self.started = False
        self.timer = None
        self.prompt_dict = {}
        self.create_tests()
        print("Test Results Initialized")

    def get_prompt_key(self, model_name, location_name, question_id):
        result = f"{model_name}_{location_name}_{question_id}"
        return result

    def get_prompt(self, model_name, location_name, question_id):
        result = self.get_prompt_key(model_name, location_name, question_id)
        return self.prompt_dict[result]

    def calculate_max_token_count(self):
        result = 0
        for model in self.config.model_list:
            if model.max_input > result:
                result = model.max_input
        return result

    def create_test_directory(self):
        now = datetime.now()
        self.date_string = now.strftime("%Y-%m-%d-%H-%M-%S")
        self.test_result_directory = os.path.join(self.config.result_directory, self.date_string)
        os.makedirs(self.test_result_directory, exist_ok=True)

    def create_tests(self):
        max_prompt_size = self.calculate_max_token_count()
        self.create_test_directory()
        self.prompt = get_prompt(max_prompt_size, self.config)
        for model in self.config.model_list:
            self.create_tests_for_model(model)

    def create_tests_for_model(self, model):
        print("creating tests for model: ", model.llm_name)
        question_list = self.prompt.question_list
        question_location_list = self.calculate_question_location_list(model.max_input)
        model_results = self.add_model(model.llm_name, question_location_list, question_list,
                                       self.config.repeat_question_limerick_count)
        for question in question_list:
            for location in question_location_list:
                prompt_text, limericks_used_count = self.prompt.build_text_from_limerick_list(question, location,
                                                                                              model.max_input,
                                                                                              self.config.repeat_question_limerick_count)
                prompt_text += "\n\n" + question.question
                model_results.set_limerick_count_in_prompt(
                    limericks_used_count + self.config.repeat_question_limerick_count)
                self.prompt_dict[self.get_prompt_key(model.llm_name, location, question.id)] = prompt_text
                self.write_prompt_text_to_file(model_results, prompt_text, str(location), str(question.id))

    def write_prompt_text_to_file(self, model_results, prompt_text, location, question_id):
        if self.config.write_prompt_text_to_file:
            file_name = "p_" + location + "_" + question_id + ".txt"
            file_path = os.path.join(model_results.directory, file_name)
            with open(file_path, "w") as file:
                file.write(prompt_text)

    def calculate_question_location_list(self, max_input):
        result = []
        increment = round(max_input / self.config.location_count)
        max_input = round(max_input * 0.90)
        initial_location = last_location = round(max_input * 0.01)
        result.append(initial_location)
        for i in range(1, self.config.location_count):
            result.append(last_location + increment)
            last_location = result[-1]
        return result

    def add_action(self, action):
        with self.action_list_lock:
            self.action_list.append(action)

    def reset_and_return_actions(self):
        with self.action_list_lock:
            result = self.action_list
            self.action_list = []
        return result

    def execute_actions(self):
        actions = self.reset_and_return_actions()
        for action in actions:
            try:
                action.execute()
            except Exception as e:
                print("Exception executing action: ", str(e))
                pass

    def start(self):
        futures_list = []
        for model_results in self.model_results_list:
            #need a thread pool for each model to keep one model from blocking another
            thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.config.test_thread_count)
            futures_list += model_results.run_trials(thread_pool, self,
                                                     self.config.get_model(model_results.model_name), self.evaluator)
        self.started = True
        self.timer = threading.Timer(interval=STATUS_REPORT_INTERVAL, function=self.update_and_report_status)
        self.timer.start()
        return futures_list

    def get_model_results(self, model_name):
        result = None
        for model_results in self.model_results_list:
            if model_results.model_name == model_name:
                result = model_results
                break
        return result

    def get_model_results_directory(self, model_name):
        model_result = self.get_model_results(model_name)
        result = model_result.directory
        return result

    def copy_model_results_list(self):
        result = None
        result = copy.deepcopy(self.model_results_list)
        return result

    def add_model(self, model_name, location_list, question_list, repeat_question_limerick_count):
        directory = os.path.join(self.test_result_directory, model_name)
        model_results = ModelResults.create(self.date_string, directory, model_name, location_list, question_list,
                                            repeat_question_limerick_count, self.config.trials,
                                            self.config.evaluator_model_list)
        self.model_results_list.append(model_results)
        return model_results

    def set_test_result(self, model_name, location_name, question_id, trial_number, generated_answer):
        action = SetTestResultAction(self, model_name, location_name, question_id, trial_number, generated_answer)
        self.add_action(action)

    def set_evaluator_result(self, model_name, location_name, question_id, trial_number, evaluator_model_name, passed):
        action = SetEvaluatorResultAction(self, model_name, location_name, question_id, trial_number,
                                          evaluator_model_name, passed)
        self.add_action(action)

    def add_test_exception(self, model_name, location_name, question_id, trial_number, attempt, exception):
        action = AddTestExceptionAction(self, model_name, location_name, question_id, trial_number, attempt, exception)
        self.add_action(action)

    def add_evaluation_exception(self, model_name, location_name, question_id, trial_number, evaluation_model_name,
                                 attempt, exception):
        action = AddEvaluationExceptionAction(self, model_name, location_name, question_id, trial_number,
                                              evaluation_model_name, attempt, exception)
        self.add_action(action)

    def calculate_scores(self):
        for model_result in self.model_results_list:
            model_result.calculate_scores()

    def update_and_report_status(self):
        if not self.started:
            return
        self.execute_actions()
        current_results_list = self.model_results_list
        model_status_list = []
        finished = True
        for model_results in current_results_list:
            status_report = model_results.calculate_scores()
            if not status_report.is_finished():
                finished = False
            model_status_list.append(status_report)
        if not finished:
            self.write_full_results(current_results_list)
            self.timer = threading.Timer(interval=STATUS_REPORT_INTERVAL, function=self.update_and_report_status)
            self.timer.start()
        else:
            model_score_list = []
            for model_results in current_results_list:
                location_scores = model_results.get_location_scores()
                model_score = ModelScore(model_results.model_name, model_results.date_string,
                                         model_results.repeat_question_limerick_count,
                                         model_results.limerick_count_in_prompt, location_scores,
                                         model_results.number_of_trials_per_location)
                plot_name = model_results.model_name + "_trial_plot.png"
                plot_file_name = os.path.join(self.test_result_directory, plot_name)
                model_score.write_trial_plot(plot_file_name)
                plot_name = model_results.model_name + "_question_plot.png"
                plot_file_name = os.path.join(self.test_result_directory, plot_name)
                model_score.write_question_plot(plot_file_name)
                model_score_list.append(model_score)
            test_model_scores = TestModelScores(model_score_list)
            test_model_scores.write_to_file(os.path.join(self.test_result_directory, "model_scores.json"))
            self.write_full_results(current_results_list)
            print("All tests are finished")

    def write_full_results(self, results_list):
        results_list = copy.deepcopy(results_list)
        for model_results in results_list:
            full_results_name = model_results.model_name + "_full_results.json"
            file_name = os.path.join(self.test_result_directory, full_results_name)
            with open(file_name, "w") as file:
                results_dict = model_results.to_dict()
                json.dump(results_dict, file, indent=4)


if __name__ == '__main__':
    model_scores_path = os.environ.get("MODEL_SCORES")
    with open(model_scores_path, "r") as file:
        model_scores_dict = json.load(file)
        test_model_scores = TestModelScores.from_dict(model_scores_dict)
        for model_score in test_model_scores.model_scores:
            print("Model: ", model_score.model_name)
            plot_name = "test_trial_plot_" + model_score.model_name + ".png"
            model_score.write_trial_plot(plot_name)
            plot_name = "test_question_plot_" + model_score.model_name + "_"
            model_score.write_question_plot(plot_name)
        print("done")
        exit(0)
